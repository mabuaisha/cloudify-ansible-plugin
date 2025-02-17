# Copyright (c) 2019 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import shutil
from tempfile import mkdtemp
from uuid import uuid1
import yaml

from cloudify.manager import get_rest_client
from cloudify.exceptions import NonRecoverableError, OperationRetry
try:
    from cloudify.constants import RELATIONSHIP_INSTANCE, NODE_INSTANCE
except ImportError:
    NODE_INSTANCE = 'node-instance'
    RELATIONSHIP_INSTANCE = 'relationship-instance'

from cloudify_ansible.constants import (
    BP_INCLUDES_PATH,
    WORKSPACE,
    SOURCES,
    HOSTS
)
from cloudify_ansible_sdk.sources import AnsibleSource


def handle_key_data(_data, workspace_dir):
    """Take Key Data from ansible_ssh_private_key_file and
    replace with a temp file.

    :param _data: The hosts dict (from YAML).
    :param workspace_dir: The temp dir where we are putting everything.
    :return: The hosts dict with a path to a temp file.
    """

    def recurse_dictionary(existing_dict, key='ansible_ssh_private_key_file'):
        if key not in existing_dict:
            for k, v in existing_dict.items():
                if isinstance(v, dict):
                    existing_dict[k] = recurse_dictionary(v)
        elif key in existing_dict:
            # If is_file_path is True, this has already been done.
            try:
                is_file_path = os.path.exists(existing_dict[key])
            except TypeError:
                is_file_path = False
            if not is_file_path:
                private_key_file = os.path.join(workspace_dir, str(uuid1()))
                with open(private_key_file, 'w') as outfile:
                    outfile.write(existing_dict[key])
                os.chmod(private_key_file, 0o600)
                existing_dict[key] = private_key_file
        return existing_dict
    return recurse_dictionary(_data)


def handle_file_path(file_path, _ctx):
    """Get the path to a file.

    I do this for two reasons:
      1. The Download Resource only downloads an individual file.
      Ansible Playbooks are often many files.
      2. I have not figured out how to pass a file as an in
      memory object to the PlaybookExecutor class.

    :param file_path: The `site_yaml_path` from `run`.
    :param _ctx: The Cloudify Context.
    :return: The absolute path on the manager to the file.
    """

    if not isinstance(file_path, basestring):
        raise NonRecoverableError(
            'The variable file_path {0} is a {1},'
            'expected a string.'.format(file_path, type(file_path)))
    if not getattr(_ctx, '_local', False):
        file_path = \
            BP_INCLUDES_PATH.format(
                tenant=_ctx.tenant_name,
                blueprint=_ctx.blueprint.id,
                relative_path=file_path)
    if os.path.exists(file_path):
        return file_path
    raise NonRecoverableError(
        'File path {0} does not exist.'.format(file_path))


def _get_instance(_ctx):
    if _ctx.type == RELATIONSHIP_INSTANCE:
        return _ctx.source.instance
    else:  # _ctx.type == NODE_INSTANCE
        return _ctx.instance


def _get_node(_ctx):
    if _ctx.type == RELATIONSHIP_INSTANCE:
        return _ctx.source.node
    else:  # _ctx.type == NODE_INSTANCE
        return _ctx.node


def handle_site_yaml(site_yaml_path, _ctx):
    """ Create an absolute local path to the site.yaml.

    :param site_yaml_path: Relative to the blueprint.
    :param _ctx: The Cloudify context.
    :return: The final absolute path on the system to the site.yaml.
    """

    site_yaml_real_path = os.path.abspath(
        handle_file_path(site_yaml_path, _ctx))
    site_yaml_real_dir = os.path.dirname(site_yaml_real_path)
    site_yaml_real_name = os.path.basename(site_yaml_real_path)
    site_yaml_new_dir = os.path.join(
        _get_instance(_ctx).runtime_properties[WORKSPACE], 'playbook')
    shutil.copytree(site_yaml_real_dir, site_yaml_new_dir)
    site_yaml_final_path = os.path.join(site_yaml_new_dir, site_yaml_real_name)
    with open(site_yaml_final_path, 'r') as infile:
        _ctx.logger.debug('Contents site.yaml:\n {0}'.format(infile.read()))
    return site_yaml_final_path


def handle_sources(data, site_yaml_abspath, _ctx):
    """Allow users to provide a path to a hosts file
    or to generate hosts dynamically,
    which is more comfortable for Cloudify users.

    :param data: Either a dict (from YAML)
        or a path to a conventional Ansible file.
    :param site_yaml_abspath: This is the path to the site yaml folder.
    :param _ctx: The Cloudify context.
    :return: The final path of the hosts file that
        was either provided or generated.
    """

    hosts_abspath = os.path.join(os.path.dirname(site_yaml_abspath), HOSTS)
    if isinstance(data, dict):
        data = handle_key_data(
            data, _get_instance(_ctx).runtime_properties[WORKSPACE])
        if os.path.exists(hosts_abspath):
            _ctx.logger.error(
                'Hosts data was provided but {0} already exists. '
                'Overwriting existing file.'.format(hosts_abspath))
        with open(hosts_abspath, 'w') as outfile:
            _ctx.logger.info(
                'Writing this data to temp file: {0}'.format(data))
            yaml.safe_dump(data, outfile, default_flow_style=False)
    elif isinstance(data, basestring):
        hosts_abspath = handle_source_from_string(data, _ctx, hosts_abspath)
    with open(hosts_abspath, 'r') as infile:
        _ctx.logger.debug('Contents hosts:\n {0}'.format(infile.read()))
    return hosts_abspath


def handle_source_from_string(filepath, _ctx, new_inventory_path):
    if '\n' not in filepath:
        # We are assuming that there is no file path with \n in it.
        # We are also assuming that there are multiple lines in an
        # inventory file :(
        try:
            open(filepath, 'r')
            # The file already exists on the system.
            return filepath
        except IOError:
            # The file doesn't exist on the system, so we need to download it.
            _ctx.download_resource(filepath, new_inventory_path)
    else:
        with open(new_inventory_path, 'w') as outfile:
            _ctx.logger.info(
                'Writing this data to temp file: {0}'.format(
                    new_inventory_path))
            outfile.write(filepath)
    return new_inventory_path


def create_playbook_workspace(ctx):
    """ Create a temporary folder, so that we don't overwrite fields.

    :param ctx: The Cloudify context.
    :return:
    """

    _get_instance(ctx).runtime_properties[WORKSPACE] = mkdtemp()


def delete_playbook_workspace(ctx):
    """Delete the temporary folder.

    :param ctx: The Cloudify context.
    :return:
    """

    directory = _get_instance(ctx).runtime_properties.get(WORKSPACE)
    if directory and os.path.exists(directory):
        shutil.rmtree(directory)


def get_source_config_from_ctx(_ctx,
                               group_name=None,
                               hostname=None,
                               host_config=None,
                               sources=None):

    """Generate a source config from CTX.

    :param _ctx: Either a NodeInstance or a RelationshipInstance ctx.
    :param group_name: User's override value, like 'webservers'.
    :param hostname: User's override value, like 'web'.
    :param host_config: User's override value. Like:
       {
           'ansible_host': '127.0.0.1',
           'ansible_user': 'ubuntu',
       }
    :return:
    """

    sources = sources or {}
    if _ctx.type == NODE_INSTANCE and \
            'cloudify.nodes.Compute' not in _ctx.node.type_hierarchy and \
            _ctx.instance.runtime_properties.get(SOURCES):
        return AnsibleSource(_ctx.instance.runtime_properties[SOURCES]).config
    elif _ctx.type == RELATIONSHIP_INSTANCE:
        host_config = host_config or \
            get_host_config_from_compute_node(_ctx.target)
        group_name, hostname = \
            get_group_name_and_hostname(
                _ctx.target, group_name, hostname)
        additional_node_groups = get_additional_node_groups(
            _ctx.target.node.name, _ctx.deployment.id)
    else:
        host_config = host_config or \
            get_host_config_from_compute_node(_ctx)
        group_name, hostname = \
            get_group_name_and_hostname(
                _ctx, group_name, hostname)
        additional_node_groups = get_additional_node_groups(
            _get_node(_ctx).name, _ctx.deployment.id)
    if '-o StrictHostKeyChecking=no' not in \
            host_config.get('ansible_ssh_common_args', ''):
        _ctx.logger.warn(
            'This value {0} is not included in Ansible Configuration. '
            'This is required for automating host key approval.'.format(
                {'ansible_ssh_common_args': '-o StrictHostKeyChecking=no'}))
    hosts = {
        hostname: host_config
    }
    sources[group_name] = {
        HOSTS: hosts
    }
    for additional_group in additional_node_groups:
        sources[additional_group] = {HOSTS: {hostname: None}}
    return AnsibleSource(sources).config


def update_sources_from_target(new_sources_dict, _ctx):
    # get source sources
    current_sources_dict = _ctx.source.instance.runtime_properties.get(
        SOURCES, {})
    current_sources = AnsibleSource(current_sources_dict)
    # get target sources
    new_sources = AnsibleSource(new_sources_dict)
    # merge sources
    current_sources.merge_source(new_sources)
    # save sources to source node
    _ctx.source.instance.runtime_properties[SOURCES] = current_sources.config
    return current_sources.config


def cleanup_sources_from_target(new_sources_dict, _ctx):
    # get source sources
    current_sources_dict = _ctx.source.instance.runtime_properties.get(
        SOURCES, {})
    current_sources = AnsibleSource(current_sources_dict)
    # get target sources
    new_sources = AnsibleSource(new_sources_dict)
    # merge sources
    current_sources.remove_source(new_sources)
    # save sources to source node
    _ctx.source.instance.runtime_properties[SOURCES] = current_sources.config
    return current_sources.config


def get_remerged_config_sources(_ctx, kwargs):
    if _ctx.type == RELATIONSHIP_INSTANCE:
        group_name = kwargs.get('group_name')
        hostname = kwargs.get('hostname')
        host_config = kwargs.get('host_config', {})
        # get target sources
        new_sources_dict = get_source_config_from_ctx(
            _ctx, group_name, hostname, host_config)
        # merged sources
        merged_sources = update_sources_from_target(new_sources_dict, _ctx)
        return AnsibleSource(merged_sources).config
    else:
        return get_source_config_from_ctx(_ctx)


def get_group_name_and_hostname(_ctx, group_name=None, hostname=None):
    """We allow a user to extend the `cloudify.nodes.Compute` node type
    and use that as a host group name in Ansible.

    :param _ctx: Either a NodeInstance or a RelationshipInstance ctx.
    :param group_name: User's override value.
    :param hostname: User's override value.
    :return:
    """

    if not group_name and not hostname and \
            'cloudify.nodes.Compute' not in _ctx.node.type_hierarchy:
        raise NonRecoverableError(
            'No sources or group_name, or hostname was provided, '
            'and furthermore no compute node was provided '
            'to generate them from.'
        )
    group_name = group_name or _ctx.node.type
    hostname = hostname or _ctx.instance.id
    return group_name, hostname


def get_host_config_from_compute_node(_ctx):
    return {
        'ansible_host': _ctx.instance.runtime_properties.get(
            'ip', _ctx.node.properties.get('ip')),
        'ansible_user': _ctx.node.properties.get(
            'agent_config', {}).get('user'),
        'ansible_ssh_pass': _ctx.node.properties.get(
            'agent_config', {}).get('password'),
        'ansible_ssh_private_key_file':
            _ctx.node.properties.get('agent_config', {}).get('key'),
        'ansible_ssh_common_args': '-o StrictHostKeyChecking=no',
        'ansible_become': _ctx.node.properties.get('ansible_become', True)
    }


def handle_result(result, _ctx, ignore_failures=False, ignore_dark=False):
    _ctx.logger.debug('result: {0}'.format(result))
    _get_instance(_ctx).runtime_properties['result'] = result
    failures = result.get('failures')
    dark = result.get('dark')
    if failures and not ignore_failures:
        raise NonRecoverableError(
            'These Ansible nodes failed: {0}'.format(failures))
    elif dark and not ignore_dark:
        raise OperationRetry(
            'These Ansible nodes were dark: {0}'.format(dark))


def assign_environ(_vars):
    for key, value in _vars.items():
        os.environ[key] = value


def unassign_environ(_vars):
    for key in _vars.keys():
        del os.environ[key]


def get_additional_node_groups(node_name, deployment_id):
    """This enables users to reuse hosts in multiple groups."""
    groups = []
    try:
        client = get_rest_client()
    except KeyError:
        return groups
    deployment = client.deployments.get(deployment_id)
    for group_name, group in deployment.get('groups', {}).items():
        if node_name in group.get('members', []) and group_name:
            groups.append(group_name)
    return groups


def cleanup(ctx):
    """
    Unset all runtime properties from node instance when delete operation
    task if finished
    :param _ctx: Cloudify node instance which is could be an instance of
    RelationshipSubjectContext or CloudifyContext
    """
    instance = _get_instance(ctx)
    for key, _ in instance.runtime_properties.items():
        del instance.runtime_properties[key]
