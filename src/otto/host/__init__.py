"""Host abstraction, concrete host families, and transfer/terminal backends.

Exports the :class:`Host` base protocol, the concrete host classes
(:class:`RemoteHost`, :class:`UnixHost`, :class:`EmbeddedHost`,
:class:`ZephyrHost`, :class:`LocalHost`, :class:`DockerContainerHost`),
session and connection primitives (:class:`SessionManager`,
:class:`ConnectionManager`), and the transfer and terminal backend
registries (:func:`register_transfer_backend`, :func:`register_term_backend`).

Every name is exported lazily (PEP 562): ``from otto.host import LocalHost``
imports ``otto.host.local_host`` and what it needs, not the other host
families, sessions and backends this package also names. The package is the
parent of every ``otto.host.*`` module, so anything it imported eagerly would
be paid by every one of them, including ``import otto.host.os_profile`` on a
path that never builds a host. Built-in registry entries do not rely on this
package being imported either: each registry names its built-ins by reference
in its own module.

The resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..result import CommandResult as CommandResult
    from ..result import Results as Results
    from .command_frame import BashFrame as BashFrame
    from .command_frame import CommandFrame as CommandFrame
    from .command_frame import RawFrame as RawFrame
    from .command_frame import SessionMarkers as SessionMarkers
    from .command_frame import ZephyrFrame as ZephyrFrame
    from .command_frame import build_command_frame as build_command_frame
    from .command_frame import register_command_frame as register_command_frame
    from .connections import ConnectionManager as ConnectionManager
    from .connections import build_term_backend as build_term_backend
    from .connections import register_term_backend as register_term_backend
    from .dev_tool import DevTool as DevTool
    from .dev_tool import DevToolProvider as DevToolProvider
    from .dev_tool import register_dev_tool_provider as register_dev_tool_provider
    from .dev_tool import registered_dev_tool_providers as registered_dev_tool_providers
    from .docker_host import DockerContainerHost as DockerContainerHost
    from .embedded_host import EmbeddedHost as EmbeddedHost
    from .embedded_host import ZephyrHost as ZephyrHost
    from .factory import create_host_from_dict as create_host_from_dict
    from .factory import validate_host_dict as validate_host_dict
    from .file_ops import PosixFileOps as PosixFileOps
    from .host import Host as Host
    from .host import HostFilter as HostFilter
    from .host import ShellCommand as ShellCommand
    from .host import SuppressCommandOutput as SuppressCommandOutput
    from .host import is_dry_run as is_dry_run
    from .local_host import LocalHost as LocalHost
    from .os_profile import OsProfile as OsProfile
    from .os_profile import build_host_class as build_host_class
    from .os_profile import build_os_profile as build_os_profile
    from .os_profile import get_host_class as get_host_class
    from .os_profile import get_os_profile as get_os_profile
    from .os_profile import register_host_class as register_host_class
    from .os_profile import register_os_profile as register_os_profile
    from .power import CommandPowerController as CommandPowerController
    from .power import PowerController as PowerController
    from .power import PowerState as PowerState
    from .power import build_power_controller as build_power_controller
    from .power import power_control_from_spec as power_control_from_spec
    from .power import register_power_controller as register_power_controller
    from .privilege import PosixPrivilege as PosixPrivilege
    from .product import Product as Product
    from .product import ProductProvider as ProductProvider
    from .product import ShellProduct as ShellProduct
    from .product import register_product_provider as register_product_provider
    from .product import registered_product_providers as registered_product_providers
    from .remote_host import OsType as OsType
    from .remote_host import RemoteHost as RemoteHost
    from .session import Expect as Expect
    from .session import HostSession as HostSession
    from .session import LocalSession as LocalSession
    from .session import SessionManager as SessionManager
    from .session import ShellSession as ShellSession
    from .session import TelnetSession as TelnetSession
    from .toolchain import Toolchain as Toolchain
    from .toolchain import ToolchainTool as ToolchainTool
    from .transfer.base import NcListenerCheck as NcListenerCheck
    from .transfer.base import NcPortStrategy as NcPortStrategy
    from .transfer.base import TransferProgressHandler as TransferProgressHandler
    from .transfer.embedded_base import EmbeddedFileTransfer as EmbeddedFileTransfer
    from .transfer.progress import make_rich_progress_handler as make_rich_progress_handler
    from .transfer.progress import make_transfer_progress as make_transfer_progress
    from .transfer.registry import build_transfer_backend as build_transfer_backend
    from .transfer.registry import register_transfer_backend as register_transfer_backend
    from .transport import HopTransport as HopTransport
    from .transport import SshHopTransport as SshHopTransport
    from .unix_host import UnixHost as UnixHost

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "BashFrame": "otto.host.command_frame",
    "CommandFrame": "otto.host.command_frame",
    "RawFrame": "otto.host.command_frame",
    "SessionMarkers": "otto.host.command_frame",
    "ZephyrFrame": "otto.host.command_frame",
    "build_command_frame": "otto.host.command_frame",
    "register_command_frame": "otto.host.command_frame",
    "ConnectionManager": "otto.host.connections",
    "build_term_backend": "otto.host.connections",
    "register_term_backend": "otto.host.connections",
    "DevTool": "otto.host.dev_tool",
    "DevToolProvider": "otto.host.dev_tool",
    "register_dev_tool_provider": "otto.host.dev_tool",
    "registered_dev_tool_providers": "otto.host.dev_tool",
    "DockerContainerHost": "otto.host.docker_host",
    "EmbeddedHost": "otto.host.embedded_host",
    "ZephyrHost": "otto.host.embedded_host",
    "create_host_from_dict": "otto.host.factory",
    "validate_host_dict": "otto.host.factory",
    "PosixFileOps": "otto.host.file_ops",
    "Host": "otto.host.host",
    "HostFilter": "otto.host.host",
    "ShellCommand": "otto.host.host",
    "SuppressCommandOutput": "otto.host.host",
    "is_dry_run": "otto.host.host",
    "LocalHost": "otto.host.local_host",
    "OsProfile": "otto.host.os_profile",
    "build_host_class": "otto.host.os_profile",
    "build_os_profile": "otto.host.os_profile",
    "get_host_class": "otto.host.os_profile",
    "get_os_profile": "otto.host.os_profile",
    "register_host_class": "otto.host.os_profile",
    "register_os_profile": "otto.host.os_profile",
    "CommandPowerController": "otto.host.power",
    "PowerController": "otto.host.power",
    "PowerState": "otto.host.power",
    "build_power_controller": "otto.host.power",
    "power_control_from_spec": "otto.host.power",
    "register_power_controller": "otto.host.power",
    "PosixPrivilege": "otto.host.privilege",
    "Product": "otto.host.product",
    "ProductProvider": "otto.host.product",
    "ShellProduct": "otto.host.product",
    "register_product_provider": "otto.host.product",
    "registered_product_providers": "otto.host.product",
    "OsType": "otto.host.remote_host",
    "RemoteHost": "otto.host.remote_host",
    "Expect": "otto.host.session",
    "HostSession": "otto.host.session",
    "LocalSession": "otto.host.session",
    "SessionManager": "otto.host.session",
    "ShellSession": "otto.host.session",
    "TelnetSession": "otto.host.session",
    "Toolchain": "otto.host.toolchain",
    "ToolchainTool": "otto.host.toolchain",
    "NcListenerCheck": "otto.host.transfer.base",
    "NcPortStrategy": "otto.host.transfer.base",
    "TransferProgressHandler": "otto.host.transfer.base",
    "EmbeddedFileTransfer": "otto.host.transfer.embedded_base",
    "make_rich_progress_handler": "otto.host.transfer.progress",
    "make_transfer_progress": "otto.host.transfer.progress",
    "build_transfer_backend": "otto.host.transfer.registry",
    "register_transfer_backend": "otto.host.transfer.registry",
    "HopTransport": "otto.host.transport",
    "SshHopTransport": "otto.host.transport",
    "UnixHost": "otto.host.unix_host",
    "CommandResult": "otto.result",
    "Results": "otto.result",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.host's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "BashFrame",
    "CommandFrame",
    "CommandPowerController",
    "CommandResult",
    "ConnectionManager",
    "DevTool",
    "DevToolProvider",
    "DockerContainerHost",
    "EmbeddedFileTransfer",
    "EmbeddedHost",
    "Expect",
    "HopTransport",
    "Host",
    "HostFilter",
    "HostSession",
    "LocalHost",
    "LocalSession",
    "NcListenerCheck",
    "NcPortStrategy",
    "OsProfile",
    "OsType",
    "PosixFileOps",
    "PosixPrivilege",
    "PowerController",
    "PowerState",
    "Product",
    "ProductProvider",
    "RawFrame",
    "RemoteHost",
    "Results",
    "SessionManager",
    "SessionMarkers",
    "ShellCommand",
    "ShellProduct",
    "ShellSession",
    "SshHopTransport",
    "SuppressCommandOutput",
    "TelnetSession",
    "Toolchain",
    "ToolchainTool",
    "TransferProgressHandler",
    "UnixHost",
    "ZephyrFrame",
    "ZephyrHost",
    "build_command_frame",
    "build_host_class",
    "build_os_profile",
    "build_power_controller",
    "build_term_backend",
    "build_transfer_backend",
    "create_host_from_dict",
    "get_host_class",
    "get_os_profile",
    "is_dry_run",
    "make_rich_progress_handler",
    "make_transfer_progress",
    "power_control_from_spec",
    "register_command_frame",
    "register_dev_tool_provider",
    "register_host_class",
    "register_os_profile",
    "register_power_controller",
    "register_product_provider",
    "register_term_backend",
    "register_transfer_backend",
    "registered_dev_tool_providers",
    "registered_product_providers",
    "validate_host_dict",
]
