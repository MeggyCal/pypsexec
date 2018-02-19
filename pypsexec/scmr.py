import logging
import struct
import uuid

from smbprotocol.connection import NtStatus
from smbprotocol.exceptions import SMBResponseException
from smbprotocol.ioctl import CtlCode, IOCTLFlags, SMB2IOCTLRequest, \
    SMB2IOCTLResponse
from smbprotocol.open import CreateDisposition, CreateOptions, \
    FilePipePrinterAccessMask, ImpersonationLevel, ShareAccess
from smbprotocol.open import Open
from smbprotocol.tree import TreeConnect

from pypsexec.exceptions import SCMRException
from pypsexec.rpc import BindAckPDU, BindPDU, ContextElement, \
    DataRepresentationFormat, PDUException, parse_pdu, \
    PFlags, RequestPDU, ResponsePDU, SyntaxIdElement

try:
    from collections import OrderedDict
except ImportError:  # pragma: no cover
    from ordereddict import OrderedDict

log = logging.getLogger(__name__)


class ControlCode(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245921.aspx
    """
    SERVICE_CONTROL_CONTINUE = 0x00000003
    SERVICE_CONTROL_INTERROGATE = 0x00000004
    SERVICE_CONTROL_NETBINDADD = 0x00000007
    SERVICE_CONTROL_NETBINDDISABLE = 0x0000000A
    SERVICE_CONTROL_NETBINDENABLE = 0x00000009
    SERVICE_CONTROL_NETBINDREMOVE = 0x00000008
    SERVICE_CONTROL_PARAMCHANGE = 0x00000006
    SERVICE_CONTROL_PAUSE = 0x00000002
    SERVICE_CONTROL_STOP = 0x00000001


class DesiredAccess(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245853.aspx
    """
    DELETE = 0x00010000
    READ_CONTROL = 0x00020000
    WRITE_DAC = 0x00040000
    WRITE_OWNER = 0x00080000
    SERVICE_ALL_ACCESS = 0x000F01FF
    SERVICE_CHANGE_CONFIG = 0x00000002
    SERVICE_ENUMERATE_DEPENDENTS = 0x00000008
    SERVICE_INTERROGATE = 0x00000080
    SERVICE_PAUSE_CONTINUE = 0x00000040
    SERVICE_QUERY_CONFIG = 0x00000001
    SERVICE_QUERY_STATUS = 0x00000004
    SERVICE_START = 0x00000010
    SERVICE_STOP = 0x00000020
    SERVICE_USER_DEFINED_CONTROL = 0x00000100
    SERVICE_SET_STATUS = 0x00008000
    SC_MANAGER_LOCK = 0x00000008
    SC_MANAGER_CREATE_SERVICE = 0x00000002
    SC_MANAGER_ENUMERATE_SERVICE = 0x00000004
    SC_MANAGER_CONNECT = 0x00000001
    SC_MANAGER_QUERY_LOCK_STATUS = 0x00000010
    SC_MANAGER_MODIFY_BOOT_CONFIG = 0x00000020


class ServiceType(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245925.aspx
    dwServiceType
    flags
    """
    SERVICE_KERNEL_DRIVER = 0x00000001
    SERVICE_FILE_SYSTEM_DRIVER = 0x00000002
    SERVICE_WIN32_OWN_PROCESS = 0x00000010
    SERVICE_WIN32_SHARE_PROCESS = 0x00000020
    SERVICE_INTERACTIVE_PROCESS = 0x00000100


class StartType(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245925.aspx
    dwStartType
    enum
    """
    SERVICE_BOOT_START = 0x00000000
    SERVICE_SYSTEM_START = 0x00000001
    SERVICE_AUTO_START = 0x00000002
    SERVICE_DEMAND_START = 0x00000003
    SERVICE_DISABLED = 0x00000004


class ErrorControl(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245925.aspx
    dwErrorControl
    enum
    """
    SERVICE_ERROR_IGNORE = 0x00000000
    SERVICE_ERROR_NORMAL = 0x00000001
    SERVICE_ERROR_SEVERE = 0x00000002
    SERVICE_ERROR_CRITICAL = 0x00000003


class CurrentState(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245911.aspx
    dwCurrentState
    enum
    """
    SERVICE_CONTINUE_PENDING = 0x00000005
    SERVICE_PAUSE_PENDING = 0x00000006
    SERVICE_PAUSED = 0x00000007
    SERVICE_RUNNING = 0x00000004
    SERVICE_START_PENDING = 0x00000002
    SERVICE_STOP_PENDING = 0x00000003
    SERVICE_STOPPED = 0x00000001


class ControlsAccepted(object):
    """
    https://msdn.microsoft.com/en-us/library/cc245911.aspx
    dwControlsAccepted
    flags
    """
    SERVICE_ACCEPT_PARAMCHANGE = 0x00000008
    SERVICE_ACCEPT_PAUSE_CONTINUE = 0x00000002
    SERVICE_ACCEPT_SHUTDOWN = 0x00000004
    SERVICE_ACCEPT_STOP = 0x00000001
    SERVICE_ACCEPT_HARDWAREPROFILECHANGE = 0x00000020
    SERVICE_ACCEPT_POWEREVENT = 0x00000040
    SERVICE_ACCEPT_SESSIONCHANGE = 0x00000080
    SERVICE_ACCEPT_PRESHUTDOWN = 0x00000100
    SERVICE_ACCEPT_TIMECHANGE = 0x00000200
    SERVICE_ACCEPT_TRIGGEREVENT = 0x00000400


class ServiceStatus(object):

    def __init__(self):
        """
        https://msdn.microsoft.com/en-us/library/cc245911.aspx
        """
        self.service_type = None
        self.current_state = None
        self.controls_accepted = None
        self.win32_exit_code = None
        self.service_specified_exit_code = None
        self.check_point = None
        self.wait_hint = None

    def pack(self):
        bytes = struct.pack("<i", self.service_type)
        bytes += struct.pack("<i", self.current_state)
        bytes += struct.pack("<i", self.controls_accepted)
        bytes += struct.pack("<i", self.win32_exit_code)
        bytes += struct.pack("<i", self.service_specified_exit_code)
        bytes += struct.pack("<i", self.check_point)
        bytes += struct.pack("<i", self.wait_hint)
        return bytes

    def unpack(self, data):
        self.service_type = struct.unpack("<i", data[0:4])[0]
        self.current_state = struct.unpack("<i", data[4:8])[0]
        self.controls_accepted = struct.unpack("<i", data[8:12])[0]
        self.win32_exit_code = struct.unpack("<i", data[12:16])[0]
        self.service_specified_exit_code = struct.unpack("<i", data[16:20])[0]
        self.check_point = struct.unpack("<i", data[20:24])[0]
        self.wait_hint = struct.unpack("<i", data[24:28])[0]


class Service(object):

    def __init__(self, name, smb_session):
        """
        Higher-level interface over SCMR to manage Windows services. This is
        customised for the PAExec service to really just be used in that
        scenario.

        :param name: The name of the service
        :param smb_session: A connected SMB Session that can be used to connect
            to the IPC$ tree.
        """
        self.name = name
        self.smb_session = smb_session
        self.exists = None
        self.status = None

        self._handle = None
        self._scmr = None
        self._scmr_handle = None

    def open(self):
        if self._handle:
            return

        # connect to the SCMR Endpoint
        self._scmr = SCMRApi(self.smb_session)
        self._scmr.open()
        self._scmr_handle = self._scmr.open_sc_manager_w(
            self.smb_session.connection.server_name,
            None,
            DesiredAccess.SC_MANAGER_CONNECT |
            DesiredAccess.SC_MANAGER_CREATE_SERVICE |
            DesiredAccess.SC_MANAGER_ENUMERATE_SERVICE
        )

        # connect to the desired service in question
        desired_access = DesiredAccess.SERVICE_QUERY_STATUS | \
            DesiredAccess.SERVICE_START | \
            DesiredAccess.SERVICE_STOP | \
            DesiredAccess.DELETE
        try:
            self._handle = self._scmr.open_service_w(self._scmr_handle,
                                                     self.name,
                                                     desired_access)
        except SCMRException as exc:
            # 1060 is service does not exist
            if exc.return_code == 1060:
                self.exists = False
            else:
                raise exc
        else:
            self.exists = True
            self.refresh()

    def close(self):
        if self._handle:
            self._scmr.close_service_handle_w(self._handle)
            self.exists = None
            self.status = None
            self._handle = None

        if self._scmr_handle:
            self._scmr.close_service_handle_w(self._scmr_handle)
            self._scmr_handle = None

        if self._scmr:
            self._scmr.close()
            self._scmr = False

    def refresh(self):
        """
        Refreshes the service details, currently only exists and status
        are set.
        """
        if not self._handle:
            self.open()

        if not self.exists:
            return

        service_status = self._scmr.query_service_status(self._handle)
        self.status = {
            CurrentState.SERVICE_STOPPED: "stopped",
            CurrentState.SERVICE_START_PENDING: "start_pending",
            CurrentState.SERVICE_STOP_PENDING: "stop_pending",
            CurrentState.SERVICE_RUNNING: "running",
            CurrentState.SERVICE_CONTINUE_PENDING: "continue_pending",
            CurrentState.SERVICE_PAUSE_PENDING: "pause_pending",
            CurrentState.SERVICE_PAUSED: "paused"
        }[service_status.current_state]

    def start(self):
        if self.status == "running":
            return
        self._scmr.start_service_w(self._handle)
        self.refresh()

    def stop(self):
        if self.status == "stopped":
            return
        self._scmr.control_service(self._handle,
                                   ControlCode.SERVICE_CONTROL_STOP)
        self.refresh()

    def create(self, name, path):
        self._handle = self._scmr.create_service_wow64_w(
            self._scmr_handle,
            name,
            name,
            DesiredAccess.SERVICE_QUERY_STATUS | DesiredAccess.SERVICE_START |
            DesiredAccess.SERVICE_STOP | DesiredAccess.DELETE,
            ServiceType.SERVICE_WIN32_OWN_PROCESS,
            StartType.SERVICE_DEMAND_START,
            ErrorControl.SERVICE_ERROR_NORMAL,
            path,
            None,
            0,
            None,
            None,
            None
        )[1]
        self.exists = True

    def delete(self):
        self.stop()
        self._scmr.delete_service(self._handle)
        self.close()


class SCMRApi(object):

    def __init__(self, smb_session):
        # connect to the IPC tree and open a handle at svcctl
        self.tree = TreeConnect(smb_session, r"\\%s\IPC$"
                                % smb_session.connection.server_name)
        self.handle = Open(self.tree, "svcctl")
        self.call_id = 0

    def open(self):
        self.tree.connect()
        self.handle.open(ImpersonationLevel.Impersonation,
                         FilePipePrinterAccessMask.GENERIC_READ |
                         FilePipePrinterAccessMask.GENERIC_WRITE,
                         0,
                         ShareAccess.FILE_SHARE_READ |
                         ShareAccess.FILE_SHARE_WRITE |
                         ShareAccess.FILE_SHARE_DELETE,
                         CreateDisposition.FILE_OPEN,
                         CreateOptions.FILE_NON_DIRECTORY_FILE)

        # we need to bind svcctl to SCManagerW over DCE/RPC
        bind = BindPDU()
        bind['pfx_flags'].set_flag(PFlags.PFC_FIRST_FRAG)
        bind['pfx_flags'].set_flag(PFlags.PFC_LAST_FRAG)
        bind['packed_drep'] = DataRepresentationFormat()
        bind['call_id'] = self.call_id
        self.call_id += 1

        context_ndr = ContextElement()
        context_ndr['context_id'] = 0
        context_ndr['abstract_syntax'] = SyntaxIdElement()
        context_ndr['abstract_syntax']['uuid'] = \
            uuid.UUID("367ABB81-9844-35F1-AD32-98F038001003")
        context_ndr['abstract_syntax']['version'] = 2

        # https://msdn.microsoft.com/en-us/library/cc243843.aspx
        ndr_syntax = SyntaxIdElement()
        ndr_syntax['uuid'] = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")
        ndr_syntax['version'] = 2
        context_ndr['transfer_syntaxes'] = [
            ndr_syntax
        ]

        context_bind = ContextElement()
        context_bind['context_id'] = 1
        context_bind['abstract_syntax'] = SyntaxIdElement()
        context_bind['abstract_syntax']['uuid'] = \
            uuid.UUID("367ABB81-9844-35F1-AD32-98F038001003")
        context_bind['abstract_syntax']['version'] = 2

        # https://msdn.microsoft.com/en-us/library/cc243715.aspx
        # uuid prefix = 6CB71C2C-9812-4540
        # uuid prefix bytes = b'\x2c\x1c\xb7\x6c\x12\x98\x40\x45'
        # BindTimeFeatureNegotiateBitmask
        # https://msdn.microsoft.com/en-us/library/cc243884.aspx
        # SecurityContextMultiplexingSupported = 0x01
        # KeepConnectionOnOrphanSupported = 0x02
        # version number is 1
        bind_syntax = SyntaxIdElement()
        bind_syntax['uuid'] = b'\x2c\x1c\xb7\x6c\x12\x98\x40\x45' \
                              b'\x03\x00\x00\x00\x00\x00\x00\x00'
        bind_syntax['version'] = 1
        context_bind['transfer_syntaxes'] = [
            bind_syntax
        ]

        bind['context_elems'] = [
            context_ndr,
            context_bind
        ]
        bind_data = bind.pack()

        self.handle.write(bind_data)
        bind_data = self.handle.read(0, 1024)
        bind_result = parse_pdu(bind_data)
        if not isinstance(bind_result, BindAckPDU):
            raise PDUException("Expecting BindAckPDU for initial bind result "
                               "but got: %s" % str(bind_result))

    def close(self):
        self.handle.close(False)
        self.tree.disconnect()

    # SCMR Functions below

    def close_service_handle_w(self, handle):
        # https://msdn.microsoft.com/en-us/library/cc245920.aspx
        errors = {
            0: "ERROR_SUCCESS",
            0xFFFF75FE: "ERROR_SUCCESS_NOTIFY_CHANGED",
            0xFFFF75FD: "ERROR_SUCCESS_LAST_NOTIFY_CHANGED",
            6: "ERROR_INVALID_HANDLE"
        }
        opnum = 0

        res = self._invoke(opnum, handle)
        handle = res[:20]
        return_code = struct.unpack("<i", res[20:])[0]
        self._parse_error(return_code, errors, "RCloseServiceHandleW")
        return handle

    def control_service(self, service_handle, control_code):
        # https://msdn.microsoft.com/en-us/library/cc245921.aspx
        errors = {
            0: "ERROR_SUCCESS",
            5: "ERROR_ACCESS_DENIED",
            6: "ERROR_INVALID_HANDLE",
            87: "ERROR_INVALID_PARAMETER",
            1051: "ERROR_DEPENDENT_SERVICES_RUNNING",
            1052: "ERROR_INVALID_SERVICE_CONTROL",
            1053: "ERROR_SERVICE_REQUEST_TIMEOUT",
            1061: "ERROR_SERVICE_CANNOT_ACCEPT_CTRL",
            1062: "ERROR_SERVICE_NOT_ACTIVE",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 1

        data = service_handle
        data += struct.pack("<i", control_code)

        res = self._invoke(opnum, data)
        return_code = struct.unpack("<i", res[-4:])[0]
        self._parse_error(return_code, errors, "RQueryServiceStatus")

        service_status = ServiceStatus()
        service_status.unpack(res[:-4])

        return service_status

    def delete_service(self, service_handle):
        # https://msdn.microsoft.com/en-us/library/cc245926.aspx
        errors = {
            0: "ERROR_SUCCESS",
            5: "ERROR_ACCESS_DENIED",
            6: "ERROR_INVALID_HANDLE",
            1072: "ERROR_SERVICE_MAKRED_FOR_DELETE",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 2

        res = self._invoke(opnum, service_handle)
        return_code = struct.unpack("<i", res)[0]
        self._parse_error(return_code, errors, "RDeleteService")

    def query_service_status(self, service_handle):
        # https://msdn.microsoft.com/en-us/library/cc245952.aspx
        errors = {
            0: "ERROR_SUCCESS",
            3: "ERROR_PATH_NOT_FOUND",
            5: "ERROR_ACCESS_DENIED",
            6: "ERROR_INVALID_HANDLE",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 6

        res = self._invoke(opnum, service_handle)
        return_code = struct.unpack("<i", res[-4:])[0]
        self._parse_error(return_code, errors, "RQueryServiceStatus")

        service_status = ServiceStatus()
        service_status.unpack(res[:-4])

        return service_status

    def open_sc_manager_w(self, machine_name, database_name, desired_access):
        # https://msdn.microsoft.com/en-us/library/cc245942.aspx
        errors = {
            0: "ERROR_SUCCESS",
            5: "ERROR_ACCESS_DENIED",
            123: "ERROR_INVALID_NAME",
            1065: "ERROR_DATABASE_DOES_NOT_EXIST",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 15

        data = self._marshal_string(machine_name, True)
        data += self._marshal_string(database_name)
        data += struct.pack("<i", desired_access)

        res = self._invoke(opnum, data)
        server_handle = res[:20]
        return_code = struct.unpack("<i", res[20:])[0]
        self._parse_error(return_code, errors, "ROpenSCManagerW")
        return server_handle

    def open_service_w(self, server_handle, service_name, desired_access):
        # https://msdn.microsoft.com/en-us/library/cc245944.aspx
        errors = {
            0: "ERROR_SUCCESS",
            6: "ERROR_INVALID_HANDLE",
            123: "ERROR_INVALID_NAME",
            1060: "ERROR_SERVICE_DOES_NOT_EXIST",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 16

        data = server_handle
        data += self._marshal_string(service_name)
        data += b"\x00\x00"  # TODO: figure out why this is needed
        data += struct.pack("<i", desired_access)

        res = self._invoke(opnum, data)
        service_handle = res[:20]
        return_code = struct.unpack("<i", res[20:])[0]
        self._parse_error(return_code, errors, "ROpenServiceW")
        return service_handle

    def start_service_w(self, service_handle, *args):
        errors = {
            0: "ERROR_SUCCESS",
            2: "ERROR_FILE_NOT_FOUND",
            3: "ERROR_PATH_NOT_FOUND",
            5: "ERROR_ACCESS_DENIED",
            6: "ERROR_INVALID_HANDLE",
            87: "ERROR_INVALID_PARAMETER",
            1053: "ERROR_SERVICE_REQUEST_TIMEOUT",
            1054: "ERROR_SERVICE_NO_THREAD",
            1055: "ERROR_SERVICE_DATABASE_LOCKED",
            1056: "ERROR_SERVICE_ALREADY_RUNNING",
            1058: "ERROR_SERVICE_DISABLED",
            1068: "ERROR_SERVICE_DEPENDENCY_FAIL",
            1069: "ERROR_SERVICE_LOGON_FAILED",
            1072: "ERROR_SERVICE_MARKED_FOR_DELETE",
            1075: "ERROR_SERVICE_DEPENDENCY_DELETED",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 19

        data = service_handle
        data += struct.pack("<i", len(args))
        data += b"".join([self._marshal_string(arg) for arg in args])
        data += b"\x00" * 4  # terminal arg list

        res = self._invoke(opnum, data)
        return_code = struct.unpack("<i", res)[0]
        self._parse_error(return_code, errors, "RStartServiceW")

    def create_service_wow64_w(self, server_handle, service_name,
                               display_name, desired_access, service_type,
                               start_type, error_control, path,
                               load_order_group, tag_id, dependencies,
                               username, password):
        # https://msdn.microsoft.com/en-us/library/cc245925.aspx
        errors = {
            0: "ERROR_SUCCESS",
            5: "ERROR_ACCESS_DENIED",
            13: "ERROR_INVALID_DATA",
            87: "ERROR_INVALID_PARAMETER",
            123: "EEROR_INVALID_NAME",
            1057: "ERROR_INVALID_SERVICE_ACCOUNT",
            1059: "ERROR_CIRCULAR_DEPENDENCY",
            1072: "ERROR_SERVICE_MARKED_FOR_DELETE",
            1078: "ERROR_DUPLICATE_SERVICE_NAME",
            1115: "ERROR_SHUTDOWN_IN_PROGRESS"
        }
        opnum = 45

        if service_name is None:
            raise ValueError("Service name must be supplied when creating a "
                             "new service")

        data = server_handle
        data += self._marshal_string(service_name)
        data += b"\x00" * 2  # why?
        data += self._marshal_string(display_name, True)
        data += b"\x00" * 2  # why again?
        data += struct.pack("<i", desired_access)
        data += struct.pack("<i", service_type)
        data += struct.pack("<i", start_type)
        data += struct.pack("<i", error_control)
        data += self._marshal_string(path)
        data += self._marshal_string(load_order_group)
        data += struct.pack("<i", tag_id)

        # TODO: convert list of string to a byte object
        dependencies_bytes = dependencies if dependencies else b"\x00" * 6
        data += dependencies_bytes
        dependencies_length = len(dependencies) if dependencies else 0
        data += struct.pack("<i", dependencies_length)

        data += self._marshal_string(username)

        pass_bytes = self._marshal_string(password)
        data += pass_bytes
        pass_len = len(pass_bytes) if password else 0
        data += struct.pack("<i", pass_len)

        res = self._invoke(opnum, data)
        tag_id = res[0:4]
        service_handle = res[4:24]
        return_code = struct.unpack("<i", res[24:])[0]
        self._parse_error(return_code, errors, "RCreateServiceWOW64W")
        return (tag_id, service_handle)

    def _invoke(self, opnum, data):
        req = RequestPDU()
        req['pfx_flags'].set_flag(PFlags.PFC_FIRST_FRAG)
        req['pfx_flags'].set_flag(PFlags.PFC_LAST_FRAG)
        req['packed_drep'] = DataRepresentationFormat()
        req['call_id'] = self.call_id
        self.call_id += 1

        req['opnum'] = opnum
        req['stub_data'] = data

        ioctl_request = SMB2IOCTLRequest()
        ioctl_request['ctl_code'] = CtlCode.FSCTL_PIPE_TRANSCEIVE
        ioctl_request['file_id'] = self.handle.file_id
        ioctl_request['max_output_response'] = 1024
        ioctl_request['flags'] = IOCTLFlags.SMB2_0_IOCTL_IS_FSCTL
        ioctl_request['buffer'] = req

        session_id = self.tree.session.session_id
        tree_id = self.tree.tree_connect_id
        request = self.tree.session.connection.send(ioctl_request,
                                                    sid=session_id,
                                                    tid=tree_id)
        while True:
            try:
                resp = self.tree.session.connection.receive(request)
            except SMBResponseException as exc:
                # try again if the status is pending
                if exc.status != NtStatus.STATUS_PENDING:
                    raise exc
            else:
                break

        ioctl_resp = SMB2IOCTLResponse()
        ioctl_resp.unpack(resp['data'].get_value())

        pdu_resp = parse_pdu(ioctl_resp['buffer'].get_value())
        if not isinstance(pdu_resp, ResponsePDU):
            raise PDUException("Expecting ResponsePDU for opnum %d response "
                               "but got: %s" % (opnum, str(pdu_resp)))

        return pdu_resp['stub_data'].get_value()

    def _parse_error(self, return_code, known_errors, function_name):
        error_string = known_errors.get(return_code, "ERROR_UNKNOWN")
        if not error_string.startswith("ERROR_SUCCESS"):
            raise SCMRException(function_name, return_code, error_string)

    def _marshal_string(self, string, referent_required=False):
        # return NULL Pointer for a null string
        if not string:
            return b"\x00" * 4

        unicode_string = string.encode("utf-16-le") + b"\x00\x00"
        max_count = struct.pack("<i", int(len(unicode_string) / 2))
        offset = b"\x00" * 4
        actual_count = max_count
        bytes = max_count + offset + actual_count + unicode_string

        # TODO: understand referent_id more
        if referent_required:
            return b"\x00\x00\x00\x01" + bytes
        else:
            return bytes
