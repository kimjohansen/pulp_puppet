# -*- coding: utf-8 -*-
#
# Copyright © 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

import gdbm
import logging
import os.path

from pulp.server.managers.consumer.bind import BindManager
from pulp.server.managers.repo.distributor import RepoDistributorManager
import web

from pulp_puppet.common import constants
from pulp_puppet.forge.unit import Unit

_LOGGER = logging.getLogger(__name__)


def view(consumer_id, repo_id, module_name, version=None):
    """
    produces data for the "releases.json" view

    :param consumer_id: unique ID for a consumer
    :type  consumer_id: str
    :param repo_id:     unique ID for a repo
    :type  repo_id:     str
    :param module_name: name of a module in form "author/title"
    :type  module_name: str
    :param version:     optional version
    :type  version:     str

    :return:    data structure defining dependency data for the given module and
                its download path, identical to what the puppet forge v1 API
                generates, except this structure is not yet JSON serialized
    :rtype:     dict
    """
    if repo_id == constants.FORGE_NULL_AUTH_VALUE:
        if consumer_id == constants.FORGE_NULL_AUTH_VALUE:
            # must provide either consumer ID or repo ID
            raise web.Unauthorized()
        repo_ids = get_bound_repos(consumer_id)
    else:
        repo_ids = [repo_id]
    if version:
        unit = find_version(repo_ids, module_name, version)
    else:
        unit = find_newest(repo_ids, module_name)
    if not unit:
        raise web.NotFound()
    try:
        data = unit.build_dep_metadata()
    finally:
        unit.db.close()
    return data


# this just provides a convenient way to access each config key and value from
# the following function
PROTOCOL_CONFIG_KEYS = {
    'http' : (constants.CONFIG_HTTP_DIR, constants.DEFAULT_HTTP_DIR),
    'https' : (constants.CONFIG_HTTPS_DIR, constants.DEFAULT_HTTPS_DIR),
}


def get_repo_data(repo_ids):
    """
    Find, open, and return the gdbm database file associated with each repo
    plus that repo's publish protocol

    :param repo_ids: list of repository IDs.
    :type  repo_ids: list

    :return:    dictionary where keys are repo IDs, and values are dicts that
                contain an open gdbm database under key "db", and a protocol
                under key "protocol".
    :rtype:     dict
    """
    ret = {}
    for distributor in RepoDistributorManager.find_by_repo_list(repo_ids):
        publish_protocol = _get_protocol_from_distributor(distributor)
        protocol_key, protocol_default_value = PROTOCOL_CONFIG_KEYS[publish_protocol]
        repo_path = distributor['config'].get(protocol_key, protocol_default_value)
        repo_id = distributor['repo_id']
        db_path = os.path.join(repo_path, repo_id, constants.REPO_DEPDATA_FILENAME)
        try:
            ret[repo_id] = {'db': gdbm.open(db_path, 'r'), 'protocol': publish_protocol}
        except gdbm.error:
            _LOGGER.error('failed to find dependency database for repo %s. re-publish to fix.' % repo_id)
    return ret


def _get_protocol_from_distributor(distributor):
    """
    Look at a distributor's config and determine what protocol it gets published
    for. Gives preference to https in case a distributor is configured for both.

    :param distributor: distributor as returned by
                        pulp.server.managers.RepoDistributorManager, should be
                        a dict with key 'config'
    :type  distributor: dict
    :return:
    """
    config = distributor['config']
    # look for an explicit setting for this distributor
    if config.get(constants.CONFIG_SERVE_HTTPS):
        return 'https'
    elif config.get(constants.CONFIG_SERVE_HTTP):
        return 'http'
    # look for the default
    elif constants.DEFAULT_SERVE_HTTPS:
        return 'https'
    elif constants.DEFAULT_SERVE_HTTP:
        return 'http'


def find_version(repo_ids, module_name, version):
    """
    Find a particular version of the requested module

    :param repo_ids:    IDs of repos to search in
    :type  repo_ids:    list
    :param module_name: name of module in form "author/title"
    :type  module_name: str
    :param version:     version to search for
    :type  version:     str

    :return:    Unit instance
    :rtype:     puppet.forge.unit.Unit
    """
    dbs = get_repo_data(repo_ids)
    host = get_host_and_protocol()['host']
    ret = None
    try:
        for repo_id, data in dbs.iteritems():
            units = Unit.units_from_json(module_name, data['db'], repo_id, host, data['protocol'])
            for unit in units:
                if unit.version == version:
                    ret = unit
                    break

    finally:
        # close database files we don't need to use
        if ret:
            del dbs[ret.repo_id]
        for data in dbs.itervalues():
            data['db'].close()

    return ret


def find_newest(repo_ids, module_name):
    """
    Find the newest version of the requested module

    :param repo_ids:    IDs of repos to search in
    :type  repo_ids:    list
    :param module_name: name of module in form "author/title"
    :type  module_name: str

    :return:    Unit instance, or None if not found
    :rtype:     puppet.forge.unit.Unit
    """
    dbs = get_repo_data(repo_ids)
    host = get_host_and_protocol()['host']
    ret = None
    try:
        for repo_id, data in dbs.iteritems():
            units = Unit.units_from_json(module_name, data['db'], repo_id, host, data['protocol'])
            if units:
                repo_max = max(units)
                if ret is None or repo_max > ret:
                    ret = repo_max
    finally:
        # close database files we don't need to use
        if ret:
            del dbs[ret.repo_id]
        for data in dbs.itervalues():
            data['db'].close()
    return ret


def get_host_and_protocol():
    """
    Get host and protocol from the web request and return them

    :return:    dict with keys "host" and "protocol"
    :rtype:     dict
    """
    return {
        'host' : web.ctx.host,
        'protocol' : web.ctx.protocol
    }


def get_bound_repos(consumer_id):
    """
    :param consumer_id: unique ID of a consumer
    :type  consumer_id: str

    :return:    list of repo IDs
    :rtype:     list
    """
    bindings = BindManager().find_by_consumer(consumer_id)
    repos = [binding['repo_id'] for binding in bindings if binding['distributor_id'] == constants.DISTRIBUTOR_TYPE_ID]
    return repos
