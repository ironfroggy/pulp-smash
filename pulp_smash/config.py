# coding=utf-8
"""Tools for managing information about test systems.

Pulp Smash needs to know what servers it can talk to and how to talk to those
systems. For example, it needs to know the protocol, hostname and port of a
Pulp server (e.g. 'https://example.com:250') and how to authenticate with that
server. :class:`pulp_smash.config.PulpSmashConfig` eases the task of managing
that information.
"""
import collections
import json
import os
import warnings
from copy import deepcopy
from urllib.parse import urlparse

from packaging.version import Version
from xdg import BaseDirectory

from pulp_smash import exceptions


# `get_config` uses this as a cache. It is intentionally a global. This design
# lets us do interesting things like flush the cache at run time or completely
# avoid a config file by fetching values from the UI.
_CONFIG = None

# Set of roles that are required on a Pulp deployment
REQUIRED_ROLES = {
    'amqp broker',
    'api',
    'mongod',
    'pulp celerybeat',
    'pulp resource manager',
    'pulp workers',
    'shell',
}

# Set of roles that can be present or not on a Pulp deployment
OPTIONAL_ROLES = {
    'pulp cli',
    'squid',
}

# Set of all available roles on a Pulp deployment
ROLES = REQUIRED_ROLES.union(OPTIONAL_ROLES)

# Set of expected amqp services
AMQP_SERVICES = {'qpidd', 'rabbitmq'}


def _public_attrs(obj):
    """Return a copy of the public elements in ``vars(obj)``."""
    return {
        key: val for key, val in vars(obj).copy().items()
        if not key.startswith('_')
    }


def get_config():
    """Return a copy of the global ``PulpSmashConfig`` object.

    This method makes use of a cache. If the cache is empty, the configuration
    file is parsed and the cache is populated. Otherwise, a copy of the cached
    configuration object is returned.

    :returns: A copy of the global server configuration object.
    :rtype: pulp_smash.config.PulpSmashConfig
    """
    global _CONFIG  # pylint:disable=global-statement
    if _CONFIG is None:
        _CONFIG = PulpSmashConfig().read()
    return deepcopy(_CONFIG)


# Representation of a system and its roles."""
PulpSystem = collections.namedtuple('PulpSystem', 'hostname roles')


class PulpSmashConfig(object):
    """Information about a Pulp deployment.

    This object stores a set of information about a Pulp deployment and its
    systems. A typical usage of this object is as follows:

    >>> import requests
    >>> from pulp_smash.config import PulpSmashConfig
    >>> cfg = PulpSmashConfig(
    ...     pulp_auth=('username', 'password'),
    ...     pulp_version='2.12.2',
    ...     systems=[
    ...         PulpSystem(
    ...             hostname='pulp.example.com',
    ...             roles={
    ...                 'amqp broker': {'service': 'qpidd'},
    ...                 'api': {
    ...                     'scheme': 'https',
    ...                     'verify': False,  # Disable SSL verification
    ...                 },
    ...                 'mongod': {},
    ...                 'pulp cli': {},
    ...                 'pulp celerybeat': {},
    ...                 'pulp resource manager': {},
    ...                 'pulp workers': {},
    ...                 'shell': {
    ...                     'transport': 'ssh',
    ...                 },
    ...             },
    ...         )
    ...     ]
    ... )
    >>> response = requests.post(
    ...     cfg.base_url + '/pulp/api/v2/actions/login/',
    ...     **cfg.get_requests_kwargs()
    ... )

    All methods dealing with files obey the `XDG Base Directory Specification
    <http://standards.freedesktop.org/basedir-spec/basedir-spec-latest.html>`_.
    Please read the specification for insight into the logic of those methods.

    In addition, the file-facing methods respect the ``PULP_SMASH_CONFIG_FILE``
    environment variable. By default, the methods work with a file named
    ``settings.json``, but the environment variable overrides that default. If
    set, the environment variable should be a file name like ``settings2.json``
    (or a relative path), *not* an absolute path.

    :param pulp_auth: A two-tuple. Credentials to use when communicating with
        the server. For example: ``('username', 'password')``.
    :param pulp_version: A string, such as '1.2' or '0.8.rc3'. Defaults to
        '1!0' (epoch 1, version 0). Must be compatible with the `packaging`_
        library's ``packaging.version.Version`` class.
    :param systems: A list of `pulp_smash.config.PulpSystem`. Mapping every
        system on a given Pulp deployment.

    .. _packaging: https://packaging.pypa.io/en/latest/
    """

    def __init__(self, pulp_auth=None, pulp_version=None, systems=None):
        """Initialize this object with needed instance attributes."""
        self.pulp_auth = pulp_auth
        self.pulp_version = pulp_version
        self.systems = systems
        if self.systems is None:
            self.systems = []
        self._xdg_config_file = os.environ.get(
            'PULP_SMASH_CONFIG_FILE',
            'settings.json'
        )
        self._xdg_config_dir = 'pulp_smash'

    def __repr__(self):  # noqa
        attrs = _public_attrs(self)
        attrs['pulp_version'] = type('')(attrs['pulp_version'])
        str_kwargs = ', '.join(
            '{}={}'.format(key, repr(value)) for key, value in attrs.items()
        )
        return '{}({})'.format(type(self).__name__, str_kwargs)

    def read(self, xdg_config_file=None, xdg_config_dir=None):
        """Read a configuration file.

        :param xdg_config_file: A string. The name of the file to manipulate.
        :param xdg_config_dir: A string. The XDG configuration directory in
            which the configuration file resides.
        :returns: A new :class:`pulp_smash.config.PulpSmashConfig` object. The
            current object is not modified by this method.
        :rtype: PulpSmashConfig
        :raises: ``warnings.DecprecationWarning`` if the configuration file
            uses the old format instead of the new system with roles based
            format.
        """
        # Read the configuration file.
        if xdg_config_file is None:
            xdg_config_file = self._xdg_config_file
        if xdg_config_dir is None:
            xdg_config_dir = self._xdg_config_dir
        path = _get_config_file_path(xdg_config_dir, xdg_config_file)
        with open(path) as handle:
            config_file = json.load(handle)

        pulp = config_file.get('pulp', {})
        pulp_auth = pulp.get('auth', ['admin', 'admin'])
        pulp_version = Version(pulp.get('version', '1!0'))

        systems = []
        if 'systems' not in config_file:
            # We could use textwrap.wrap() on the message, but that makes log
            # files harder to grep.
            warnings.warn(
                (
                    'Pulp Smash configuration file should follow the systems '
                    'with roles format. But the configuration file at "{0}" '
                    'appears to be following the old configuration file '
                    'format. Consider updating the configuration file to the '
                    'new system with roles format, run python3 -m pulp_smash '
                    'for more information.'
                    .format(path)
                ),
                DeprecationWarning
            )
            parsed_url = urlparse(pulp.get('base_url'))
            systems.append(PulpSystem(
                parsed_url.hostname or '',
                {
                    'amqp broker': {'service': 'qpidd'},
                    'api': {
                        'scheme': parsed_url.scheme,
                        'verify': pulp.get('verify'),
                    },
                    'mongod': {},
                    'pulp cli': {},
                    'pulp celerybeat': {},
                    'pulp resource manager': {},
                    'pulp workers': {},
                    'shell': {
                        'transport': pulp.get('cli_transport'),
                    },
                    'squid': {},
                }
            ))
        else:
            for system in config_file.get('systems', []):
                systems.append(PulpSystem(**system))
        return PulpSmashConfig(pulp_auth, pulp_version, systems)

    def get_systems(self, role):
        """Return a list of systems fulfilling the given role.

        :param role: The role to filter the available systems, see
            `pulp_smash.config.ROLES` for more information.
        """
        if role not in ROLES:
            raise ValueError(
                'The given role, {}, is not recognized. Valid roles are: {}'
                .format(role, ROLES)
            )
        return [system for system in self.systems if role in system.roles]

    @staticmethod
    def services_for_roles(roles):
        """Return the services based on the roles."""
        services = []
        for role in roles:
            if role == 'amqp broker':
                service = roles[role].get('service')
                if service in AMQP_SERVICES:
                    services.append(service)
            elif role == 'api':
                services.append('httpd')
            elif role in (
                    'pulp celerybeat',
                    'pulp resource manager',
                    'pulp workers',
            ):
                services.append(role.replace(' ', '_'))
            elif role in ('mongod', 'squid'):
                services.append(role)
            else:
                continue
        return set(services)

    @property
    def base_url(self):
        """Map old config base_url to the system with api role."""
        api_system = self.get_systems('api')[0]
        return '{}://{}/'.format(
            api_system.roles['api']['scheme'], api_system.hostname)

    def get_base_url(self, pulp_system=None):
        """Generate the base URL for a given ``pulp_sytem``.

        If ``pulp_system`` is ``None`` then the first system found with the
        ``api`` role will be chosen.
        """
        if not pulp_system:
            pulp_system = self.get_systems('api')[0]
        return '{}://{}/'.format(
            pulp_system.roles['api'].get('scheme', 'https'),
            pulp_system.hostname
        )

    @property
    def version(self):
        """Map old config version to the pulp_version."""
        return self.pulp_version

    @property
    def cli_transport(self):
        """Map old config cli_transport to the system with shell role."""
        return self.get_systems('shell')[0].roles['shell']['transport']

    @property
    def verify(self):
        """Map old config verify to the system with api role."""
        return self.get_systems('api')[0].roles['api'].get('verify')

    def get_requests_kwargs(self, pulp_system=None):
        """Get kwargs for use by the Requests functions.

        This method returns a dict of attributes that can be unpacked and used
        as kwargs via the ``**`` operator. For example:

        >>> cfg = PulpSmashConfig().read()
        >>> requests.get(cfg.base_url + '…', **cfg.get_requests_kwargs())

        This method is useful because client code may not know which attributes
        should be passed from a ``PulpSmashConfig`` object to Requests.
        Consider that the example above could also be written like this:

        >>> cfg = PulpSmashConfig().get()
        >>> requests.get(
        ...     cfg.base_url + '…',
        ...     auth=tuple(cfg.pulp_auth),
        ...     verify=cfg.get_systems('api')[0].roles['api']['verify'],
        ... )

        But this latter approach is more fragile. The user must remember to get
        a system with api role to check for the verify config, then convert
        ``pulp_auth`` config to a tuple, and it will require maintenance if
        ``cfg`` gains or loses attributes.
        """
        if not pulp_system:
            pulp_system = self.get_systems('api')[0]
        kwargs = deepcopy(pulp_system.roles['api'])
        kwargs['auth'] = tuple(self.pulp_auth)
        kwargs.pop('scheme', None)
        return kwargs


def _get_config_file_path(xdg_config_dir, xdg_config_file):
    """Search ``XDG_CONFIG_DIRS`` for a config file and return the first found.

    Search each of the standard XDG configuration directories for a
    configuration file. Return as soon as a configuration file is found. Beware
    that by the time client code attempts to open the file, it may be gone or
    otherwise inaccessible.

    :param xdg_config_dir: A string. The name of the directory that is suffixed
        to the end of each of the ``XDG_CONFIG_DIRS`` paths.
    :param xdg_config_file: A string. The name of the configuration file that
        is being searched for.
    :returns: A string. A path to a configuration file.
    :raises pulp_smash.exceptions.ConfigFileNotFoundError: If the requested
        configuration file cannot be found.
    """
    paths = [
        os.path.join(config_dir, xdg_config_file)
        for config_dir in BaseDirectory.load_config_paths(xdg_config_dir)
    ]
    for path in paths:
        if os.path.isfile(path):
            return path
    raise exceptions.ConfigFileNotFoundError(
        'Pulp Smash is unable to find a configuration file. The following '
        '(XDG compliant) paths have been searched: ' + ', '.join(paths)
    )
