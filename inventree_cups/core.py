"""This package provides a cups printer driver."""

import cups
import logging
import threading
import time
from datetime import datetime
from tempfile import NamedTemporaryFile

from django.db import models
from django.utils.translation import gettext_lazy as _

# InvenTree imports
from report.models import LabelTemplate
from plugin import InvenTreePlugin
from plugin.mixins import MachineDriverMixin, SettingsMixin
from plugin.machine import BaseMachineType
from plugin.machine.machine_types import LabelPrinterBaseDriver, LabelPrinterMachine

from inventree_cups import PLUGIN_VERSION

# Module-level logger — all plugin logging goes through this
logger = logging.getLogger('inventree')

# Module-level lock for thread-safe CUPS connections
_cups_lock = threading.Lock()

# Connection retry configuration
_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 1


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class CupsPluginError(Exception):
    """Base exception for all CUPS plugin errors."""


class CupsConnectionError(CupsPluginError):
    """Raised when the plugin cannot connect to the CUPS server."""


class CupsPrinterError(CupsPluginError):
    """Raised when the target printer is not found or not ready."""


class CupsPrintJobError(CupsPluginError):
    """Raised when a print job fails to submit or complete."""


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class CupsLabelPlugin(InvenTreePlugin, SettingsMixin, MachineDriverMixin):
    """Cups label printer driver plugin for InvenTree."""

    AUTHOR = "wolflu05"
    DESCRIPTION = "Label printer plugin for CUPS server"
    VERSION = PLUGIN_VERSION

    # Machine registry was added in InvenTree 0.14.0, use inventree-cups-plugin 0.1.0 for older versions
    # Machine driver interface was fixed with 0.16.0 to work inside of inventree workers
    # Machine driver interface was changed in 0.18.0
    MIN_VERSION = "0.18.0"

    NAME = "InvenTree Cups Plugin"
    SLUG = "inventree-cups-plugin"
    TITLE = "InvenTree Cups Plugin"

    SETTINGS = {
        "LOG_LEVEL": {
            "name": _("Log Level"),
            "description": _("Controls logging verbosity for the CUPS plugin. Set to DEBUG for full diagnostics."),
            "choices": [
                ("DEBUG", _("Debug — Full diagnostics")),
                ("INFO", _("Info — Print events and connections")),
                ("WARNING", _("Warning — Only problems")),
                ("ERROR", _("Error — Only failures")),
            ],
            "default": "WARNING",
        },
        "LAST_STATUS": {
            "name": _("Last Status"),
            "description": _("Shows the result of the last print or connection event. Updated automatically."),
            "default": "No activity yet",
        },
    }

    def get_machine_drivers(self):
        """Register machine drivers."""
        return [CupsLabelPrinterDriver]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _update_plugin_status(message: str):
    """Update the LAST_STATUS setting visible in the plugin settings UI.

    This is a best-effort update — if the plugin instance cannot be found
    or the database is unavailable, the failure is logged but not raised.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        CupsLabelPlugin.set_setting("LAST_STATUS", f"{message}  [{timestamp}]")
    except Exception as exc:
        logger.debug(f"CUPS: Could not update LAST_STATUS setting: {exc}")


def _get_plugin_log_level() -> int:
    """Read the LOG_LEVEL setting and return the corresponding logging constant."""
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    try:
        chosen = CupsLabelPlugin.get_setting("LOG_LEVEL")
        return level_map.get(chosen, logging.WARNING)
    except Exception:
        return logging.WARNING


def _log(level: int, msg: str, *args, **kwargs):
    """Log a message only if the plugin LOG_LEVEL setting permits it."""
    plugin_level = _get_plugin_log_level()
    if level >= plugin_level:
        logger.log(level, msg, *args, **kwargs)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class CupsLabelPrinterDriver(LabelPrinterBaseDriver):
    """Cups label printing driver for InvenTree."""

    SLUG = "cups-driver"
    NAME = "Cups Driver"
    DESCRIPTION = "Cups label printing driver for InvenTree"

    def __init__(self, *args, **kwargs):
        """Initialize the CupsLabelPrinterDriver."""
        self.MACHINE_SETTINGS = {
            "SERVER": {
                "name": _("Server"),
                "description": _("IP/Hostname to connect to the cups server"),
                "default": "localhost",
                "required": True,
            },
            "PORT": {
                "name": _("Port"),
                "description": _("Port to connect to the cups server"),
                "validator": int,
                "default": 631,
                "required": True,
            },
            "USER": {
                "name": _("User"),
                "description": _("User to connect to the cups server"),
                "default": "",
            },
            "PASSWORD": {
                "name": _("Password"),
                "description": _("Password to connect to the cups server"),
                "default": "",
                "protected": True,
            },
            "ENCRYPTION": {
                "name": _("Encryption"),
                "description": _("Encryption mode for CUPS connection. 'Never' is required for SSH tunnels or local port forwarding."),
                "choices": [
                    ("always", _("Always")),
                    ("never", _("Never")),
                    ("if_requested", _("If Requested")),
                ],
                "default": "never",
            },
            "PRINTER": {
                "name": _("Printer"),
                "description": _("Printer name from CUPS server. Run 'lpstat -h <SERVER>:<PORT> -p' to list available printers."),
                "required": True,
            },
        }

        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Machine lifecycle
    # ------------------------------------------------------------------

    def init_machine(self, machine: BaseMachineType):
        """Validate CUPS connectivity and printer configuration on startup."""
        server = machine.get_setting("SERVER", "D")
        port = machine.get_setting("PORT", "D")

        try:
            conn = self._get_connection_with_retry(machine)
        except CupsConnectionError as exc:
            machine.handle_error(str(exc))
            _update_plugin_status(f"❌ Init failed — {exc}")
            return

        # Validate printer exists on the server
        printer_name = machine.get_setting("PRINTER", "D")
        if printer_name:
            try:
                self._validate_printer(conn, printer_name, server, port)
                _update_plugin_status(f"✅ Initialized — printer '{printer_name}' on {server}:{port}")
            except CupsPrinterError as exc:
                machine.handle_error(str(exc))
                _update_plugin_status(f"⚠️ Init warning — {exc}")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_connection_with_retry(self, machine: LabelPrinterMachine) -> cups.Connection:
        """Get a CUPS connection with retry logic.

        Raises:
            CupsConnectionError: If all retry attempts fail.
        """
        server = machine.get_setting("SERVER", "D") or "localhost"
        port = machine.get_setting("PORT", "D")
        user = machine.get_setting("USER", "D") or ""
        password = machine.get_setting("PASSWORD", "D") or ""

        encryption_mode = machine.get_setting("ENCRYPTION", "D")
        encryption_map = {
            "always": cups.HTTP_ENCRYPT_ALWAYS,
            "never": cups.HTTP_ENCRYPT_NEVER,
            "if_requested": cups.HTTP_ENCRYPT_IF_REQUESTED,
        }
        cups_encryption = encryption_map.get(encryption_mode, cups.HTTP_ENCRYPT_NEVER)

        try:
            port = int(port) if port else 631
        except (ValueError, TypeError):
            port = 631

        last_error = None

        for attempt in range(1, _MAX_RETRIES + 1):
            _log(logging.DEBUG, f"CUPS: Connection attempt {attempt}/{_MAX_RETRIES} to {server}:{port} (enc={encryption_mode})")

            with _cups_lock:
                try:
                    conn = cups.Connection(
                        host=server,
                        port=port,
                        encryption=cups_encryption,
                    )

                    if user:
                        cups.setUser(user)
                    if password:
                        cups.setPasswordCB(lambda p=password: p)

                    # Verify the connection is functional by listing printers
                    conn.getPrinters()

                    _log(logging.INFO, f"CUPS: Connected to {server}:{port} (attempt {attempt})")
                    return conn

                except RuntimeError as exc:
                    last_error = exc
                    _log(logging.WARNING, f"CUPS: Connection attempt {attempt}/{_MAX_RETRIES} failed — RuntimeError: {exc}")
                except Exception as exc:
                    last_error = exc
                    _log(logging.WARNING, f"CUPS: Connection attempt {attempt}/{_MAX_RETRIES} failed — {type(exc).__name__}: {exc}")

            if attempt < _MAX_RETRIES:
                _log(logging.DEBUG, f"CUPS: Retrying in {_RETRY_DELAY_SECONDS}s...")
                time.sleep(_RETRY_DELAY_SECONDS)

        raise CupsConnectionError(
            _("Cannot connect to CUPS server at %(server)s:%(port)s after %(retries)s attempts. "
              "Last error: %(error)s. "
              "Verify the server is running and reachable.") % {
                "server": server,
                "port": port,
                "retries": _MAX_RETRIES,
                "error": last_error,
            }
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_printer(conn: cups.Connection, printer_name: str, server: str, port) -> None:
        """Validate that the printer exists and is in a usable state.

        Raises:
            CupsPrinterError: If the printer is missing or not ready.
        """
        try:
            available = conn.getPrinters()
        except Exception as exc:
            raise CupsPrinterError(
                _("Failed to list printers on %(server)s:%(port)s — %(error)s") % {
                    "server": server,
                    "port": port,
                    "error": exc,
                }
            ) from exc

        if printer_name not in available:
            printer_list = ", ".join(sorted(available.keys())) if available else _("none")
            raise CupsPrinterError(
                _("Printer '%(printer)s' not found on CUPS server at %(server)s:%(port)s. "
                  "Available printers: %(available)s. "
                  "Check the printer name in machine settings.") % {
                    "printer": printer_name,
                    "server": server,
                    "port": port,
                    "available": printer_list,
                }
            )

        # Check printer state (CUPS IPP: 3=idle, 4=processing, 5=stopped)
        printer_info = available[printer_name]
        state = printer_info.get("printer-state", 0)
        state_reasons = printer_info.get("printer-state-reasons", ["none"])

        if state == 5:  # stopped
            reason_str = ", ".join(state_reasons) if isinstance(state_reasons, list) else str(state_reasons)
            _log(logging.WARNING,
                 f"CUPS: Printer '{printer_name}' is stopped (state={state}, reasons={reason_str})")
            raise CupsPrinterError(
                _("Printer '%(printer)s' is stopped on %(server)s:%(port)s "
                  "(state=%(state)s, reason=%(reason)s). "
                  "Check the printer on the CUPS web interface.") % {
                    "printer": printer_name,
                    "server": server,
                    "port": port,
                    "state": state,
                    "reason": reason_str,
                }
            )

        _log(logging.DEBUG, f"CUPS: Printer '{printer_name}' is ready (state={state})")

    # ------------------------------------------------------------------
    # Printing
    # ------------------------------------------------------------------

    def print_label(
        self,
        machine: LabelPrinterMachine,
        label: LabelTemplate,
        item: models.Model,
        **kwargs,
    ) -> None:
        """Print a label via the CUPS server."""
        printer_name = machine.get_setting("PRINTER", "D")
        server = machine.get_setting("SERVER", "D") or "localhost"
        port = machine.get_setting("PORT", "D") or 631

        _log(logging.INFO, f"CUPS: Print request — label='{label.name}' printer='{printer_name}' item={item.pk}")

        machine.set_status(LabelPrinterMachine.MACHINE_STATUS.PRINTING)

        # --- Connection ---
        try:
            conn = self._get_connection_with_retry(machine)
        except CupsConnectionError as exc:
            machine.handle_error(str(exc))
            machine.set_status(LabelPrinterMachine.MACHINE_STATUS.DISCONNECTED)
            _update_plugin_status(f"❌ Connection failed — {exc}")
            return

        # --- Printer validation ---
        try:
            self._validate_printer(conn, printer_name, server, port)
        except CupsPrinterError as exc:
            machine.handle_error(str(exc))
            machine.set_status(LabelPrinterMachine.MACHINE_STATUS.DISCONNECTED)
            _update_plugin_status(f"⚠️ Printer issue — {exc}")
            return

        # --- PDF rendering ---
        try:
            pdf_data = self.render_to_pdf_data(label, item)
        except Exception as exc:
            error_msg = (
                _("Failed to render label '%(label)s' to PDF — %(error)s. "
                  "Check the label template for errors.") % {
                    "label": label.name,
                    "error": exc,
                }
            )
            _log(logging.ERROR, f"CUPS: {error_msg}")
            machine.handle_error(str(CupsPrintJobError(error_msg)))
            machine.set_status(LabelPrinterMachine.MACHINE_STATUS.OPERATIONAL)
            _update_plugin_status(f"❌ Render failed — {label.name}: {exc}")
            return

        if not pdf_data:
            error_msg = _("Label '%(label)s' rendered to empty PDF. Check the label template.") % {
                "label": label.name,
            }
            _log(logging.ERROR, f"CUPS: {error_msg}")
            machine.handle_error(str(CupsPrintJobError(error_msg)))
            machine.set_status(LabelPrinterMachine.MACHINE_STATUS.OPERATIONAL)
            _update_plugin_status(f"❌ Render failed — {label.name}: empty PDF")
            return

        # --- Submit print job ---
        with NamedTemporaryFile(suffix=".pdf") as f:
            f.write(pdf_data)
            f.flush()

            try:
                copies = kwargs.get("printing_options", {}).get("copies", 1)
                job_ids = []

                for copy_idx in range(copies):
                    job_title = f"{label.name}-{item.pk}-{copy_idx}.pdf"
                    job_id = conn.printFile(
                        printer_name,
                        f.name,
                        job_title,
                        {},
                    )
                    job_ids.append(job_id)
                    _log(logging.INFO, f"CUPS: Job {job_id} submitted — '{job_title}' → '{printer_name}'")

                machine.set_status(LabelPrinterMachine.MACHINE_STATUS.OPERATIONAL)
                _update_plugin_status(
                    f"✅ Printed OK — {copies}x '{label.name}' → '{printer_name}' "
                    f"(jobs: {', '.join(str(j) for j in job_ids)})"
                )

            except Exception as exc:
                error_msg = (
                    _("Print job failed for '%(label)s' on printer '%(printer)s' — "
                      "%(error_type)s: %(error)s. "
                      "Verify the printer is online and accepting jobs.") % {
                        "label": label.name,
                        "printer": printer_name,
                        "error_type": type(exc).__name__,
                        "error": exc,
                    }
                )
                _log(logging.ERROR, f"CUPS: {error_msg}")
                machine.set_status(LabelPrinterMachine.MACHINE_STATUS.DISCONNECTED)
                machine.handle_error(str(CupsPrintJobError(error_msg)))
                _update_plugin_status(f"❌ Print failed — '{label.name}' → '{printer_name}': {exc}")
