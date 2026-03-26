"""
CoreAudio integration for MultiSpeaker.
Handles device enumeration, aggregate device creation/destruction,
and default output management via ctypes.
"""

import ctypes
import uuid
import atexit
from ctypes import byref, sizeof, c_uint32, c_void_p
from dataclasses import dataclass

import objc
from Foundation import (
    NSMutableDictionary, NSMutableArray, NSString, NSNumber,
)

from constants import (
    AudioObjectPropertyAddress, AudioObjectPropertyListenerProc,
    kAudioObjectSystemObject, kAudioObjectPropertyElementMain,
    kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyScopeOutput,
    kAudioHardwarePropertyDevices, kAudioHardwarePropertyDefaultOutputDevice,
    kAudioObjectPropertyName, kAudioDevicePropertyDeviceUID,
    kAudioDevicePropertyStreams, kAudioDevicePropertyTransportType,
    kAudioDevicePropertyPreferredChannelsForStereo,
    kAudioDevicePropertyStreamConfiguration,
    kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE,
    AGGREGATE_UID_PREFIX, Mode,
    fourcc,
)


class CoreAudioError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"{message} (OSStatus {status})")


@dataclass
class AudioDevice:
    device_id: int
    uid: str
    name: str
    transport_type: int
    has_output: bool

    @property
    def is_bluetooth(self) -> bool:
        return self.transport_type in (
            kAudioDeviceTransportTypeBluetooth,
            kAudioDeviceTransportTypeBluetoothLE,
        )


class CoreAudioManager:
    def __init__(self):
        self._ca = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreAudio.framework/CoreAudio'
        )
        self._setup_function_signatures()
        self._aggregate_device_id: int | None = None
        self._sub_device_ids: list[str] = []
        self._original_default_output: int | None = None
        self._listener_callback = None
        self._device_change_handler = None
        atexit.register(self._cleanup)

    def _setup_function_signatures(self):
        ca = self._ca
        # AudioObjectGetPropertyDataSize
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            c_uint32,                                    # AudioObjectID
            ctypes.POINTER(AudioObjectPropertyAddress),  # address
            c_uint32,                                    # qualifier size
            c_void_p,                                    # qualifier data
            ctypes.POINTER(c_uint32),                    # out data size
        ]
        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32

        # AudioObjectGetPropertyData
        ca.AudioObjectGetPropertyData.argtypes = [
            c_uint32,
            ctypes.POINTER(AudioObjectPropertyAddress),
            c_uint32,
            c_void_p,
            ctypes.POINTER(c_uint32),  # in/out data size
            c_void_p,                  # out data
        ]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32

        # AudioObjectSetPropertyData
        ca.AudioObjectSetPropertyData.argtypes = [
            c_uint32,
            ctypes.POINTER(AudioObjectPropertyAddress),
            c_uint32,
            c_void_p,
            c_uint32,   # data size
            c_void_p,   # data
        ]
        ca.AudioObjectSetPropertyData.restype = ctypes.c_int32

        # AudioHardwareCreateAggregateDevice
        ca.AudioHardwareCreateAggregateDevice.argtypes = [
            c_void_p,                    # CFDictionaryRef
            ctypes.POINTER(c_uint32),    # out AudioDeviceID
        ]
        ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32

        # AudioHardwareDestroyAggregateDevice
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
        ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

        # AudioObjectAddPropertyListener
        ca.AudioObjectAddPropertyListener.argtypes = [
            c_uint32,
            ctypes.POINTER(AudioObjectPropertyAddress),
            AudioObjectPropertyListenerProc,
            c_void_p,
        ]
        ca.AudioObjectAddPropertyListener.restype = ctypes.c_int32

        # AudioObjectRemovePropertyListener
        ca.AudioObjectRemovePropertyListener.argtypes = [
            c_uint32,
            ctypes.POINTER(AudioObjectPropertyAddress),
            AudioObjectPropertyListenerProc,
            c_void_p,
        ]
        ca.AudioObjectRemovePropertyListener.restype = ctypes.c_int32

    # ── Property helpers ─────────────────────────────────────────────

    def _get_property_size(self, object_id, selector, scope=None):
        if scope is None:
            scope = kAudioObjectPropertyScopeGlobal
        addr = AudioObjectPropertyAddress(selector, scope, kAudioObjectPropertyElementMain)
        size = c_uint32(0)
        status = self._ca.AudioObjectGetPropertyDataSize(
            c_uint32(object_id), byref(addr), c_uint32(0), None, byref(size)
        )
        if status != 0:
            raise CoreAudioError(status, f"GetPropertyDataSize failed for {object_id}")
        return size.value

    def _get_property(self, object_id, selector, scope=None, buf_size=None, buf=None):
        if scope is None:
            scope = kAudioObjectPropertyScopeGlobal
        addr = AudioObjectPropertyAddress(selector, scope, kAudioObjectPropertyElementMain)
        if buf_size is None:
            buf_size = self._get_property_size(object_id, selector, scope)
        if buf is None:
            buf = (ctypes.c_byte * buf_size)()
        size = c_uint32(buf_size)
        status = self._ca.AudioObjectGetPropertyData(
            c_uint32(object_id), byref(addr), c_uint32(0), None, byref(size), byref(buf)
        )
        if status != 0:
            raise CoreAudioError(status, f"GetPropertyData failed for {object_id}")
        return buf, size.value

    def _set_property(self, object_id, selector, scope, data, data_size):
        addr = AudioObjectPropertyAddress(selector, scope, kAudioObjectPropertyElementMain)
        status = self._ca.AudioObjectSetPropertyData(
            c_uint32(object_id), byref(addr), c_uint32(0), None,
            c_uint32(data_size), byref(data)
        )
        if status != 0:
            raise CoreAudioError(status, f"SetPropertyData failed for {object_id}")

    def _get_cfstring_property(self, object_id, selector):
        ptr = c_void_p(0)
        addr = AudioObjectPropertyAddress(
            selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain
        )
        size = c_uint32(sizeof(c_void_p))
        status = self._ca.AudioObjectGetPropertyData(
            c_uint32(object_id), byref(addr), c_uint32(0), None, byref(size), byref(ptr)
        )
        if status != 0:
            raise CoreAudioError(status, f"GetPropertyData(CFString) failed for {object_id}")
        if ptr.value is None:
            return ""
        ns_str = objc.objc_object(c_void_p=ptr.value)
        return str(ns_str)

    # ── Device Discovery ─────────────────────────────────────────────

    def get_all_devices(self) -> list[AudioDevice]:
        size = self._get_property_size(kAudioObjectSystemObject, kAudioHardwarePropertyDevices)
        count = size // sizeof(c_uint32)
        if count == 0:
            return []
        ids = (c_uint32 * count)()
        self._get_property(
            kAudioObjectSystemObject, kAudioHardwarePropertyDevices,
            buf_size=size, buf=ids,
        )
        devices = []
        for i in range(count):
            dev_id = ids[i]
            try:
                name = self._get_cfstring_property(dev_id, kAudioObjectPropertyName)
                uid = self._get_cfstring_property(dev_id, kAudioDevicePropertyDeviceUID)
                transport = self._get_transport_type(dev_id)
                has_out = self._has_output_streams(dev_id)
                devices.append(AudioDevice(
                    device_id=dev_id, uid=uid, name=name,
                    transport_type=transport, has_output=has_out,
                ))
            except CoreAudioError:
                continue
        return devices

    def get_output_devices(self) -> list[AudioDevice]:
        return [d for d in self.get_all_devices() if d.has_output]

    def get_bluetooth_output_devices(self) -> list[AudioDevice]:
        return [d for d in self.get_all_devices() if d.is_bluetooth and d.has_output]

    def _get_transport_type(self, device_id: int) -> int:
        try:
            val = c_uint32(0)
            self._get_property(
                device_id, kAudioDevicePropertyTransportType,
                buf_size=sizeof(c_uint32), buf=val,
            )
            return val.value
        except CoreAudioError:
            return 0

    def _has_output_streams(self, device_id: int) -> bool:
        try:
            size = self._get_property_size(
                device_id, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput
            )
            return size > 0
        except CoreAudioError:
            return False

    def _get_output_channel_count(self, device_uid: str) -> int:
        """Get the number of output channels for a device identified by UID."""
        for dev in self.get_all_devices():
            if dev.uid == device_uid and dev.has_output:
                try:
                    size = self._get_property_size(
                        dev.device_id, kAudioDevicePropertyStreamConfiguration,
                        kAudioObjectPropertyScopeOutput,
                    )
                    buf = (ctypes.c_byte * size)()
                    self._get_property(
                        dev.device_id, kAudioDevicePropertyStreamConfiguration,
                        kAudioObjectPropertyScopeOutput, buf_size=size, buf=buf,
                    )
                    # AudioBufferList: first UInt32 is mNumberBuffers,
                    # then each buffer has mNumberChannels (UInt32), mDataByteSize (UInt32), mData (ptr)
                    num_buffers = ctypes.c_uint32.from_buffer_copy(buf, 0).value
                    total_channels = 0
                    offset = 4  # skip mNumberBuffers
                    ptr_size = sizeof(c_void_p)
                    for _ in range(num_buffers):
                        channels = ctypes.c_uint32.from_buffer_copy(buf, offset).value
                        total_channels += channels
                        offset += 4 + 4 + ptr_size  # mNumberChannels + mDataByteSize + mData
                    return total_channels
                except (CoreAudioError, ValueError):
                    return 2  # default assumption: stereo
        return 2

    # ── Default Output ───────────────────────────────────────────────

    def get_default_output_device_id(self) -> int:
        val = c_uint32(0)
        self._get_property(
            kAudioObjectSystemObject, kAudioHardwarePropertyDefaultOutputDevice,
            buf_size=sizeof(c_uint32), buf=val,
        )
        return val.value

    def set_default_output_device(self, device_id: int):
        val = c_uint32(device_id)
        self._set_property(
            kAudioObjectSystemObject,
            kAudioHardwarePropertyDefaultOutputDevice,
            kAudioObjectPropertyScopeGlobal,
            val, sizeof(c_uint32),
        )

    def save_original_default(self):
        self._original_default_output = self.get_default_output_device_id()

    def restore_original_default(self):
        if self._original_default_output is not None:
            try:
                self.set_default_output_device(self._original_default_output)
            except CoreAudioError:
                pass
            self._original_default_output = None

    # ── Aggregate Device ─────────────────────────────────────────────

    def create_aggregate_device(
        self,
        device_uids: list[str],
        mode: str,
        name: str = 'MultiSpeaker',
    ) -> int:
        import time
        stacked = (mode == Mode.PARTY)
        agg_uid = f"{AGGREGATE_UID_PREFIX}{mode}_{uuid.uuid4().hex[:8]}"

        # Step 1: Create aggregate device with minimal properties.
        # Sub-devices must be added separately via property for them
        # to become active on modern macOS.
        desc = NSMutableDictionary.dictionary()
        desc['uid'] = agg_uid
        desc['name'] = name
        if stacked:
            desc['stacked'] = NSNumber.numberWithInt_(1)

        raw_ptr = objc.pyobjc_id(desc)
        out_id = c_uint32(0)
        status = self._ca.AudioHardwareCreateAggregateDevice(
            c_void_p(raw_ptr), byref(out_id)
        )
        if status != 0:
            raise CoreAudioError(status, "Failed to create aggregate device")

        agg_id = out_id.value
        self._aggregate_device_id = agg_id
        self._sub_device_ids = list(device_uids)

        # Step 2: Add sub-devices one at a time via 'grup' property
        addr = AudioObjectPropertyAddress(
            fourcc('grup'), kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        cumulative_uids = NSMutableArray.array()
        for uid in device_uids:
            cumulative_uids.addObject_(uid)
            ptr_val = c_void_p(objc.pyobjc_id(cumulative_uids))
            self._ca.AudioObjectSetPropertyData(
                c_uint32(agg_id), byref(addr), c_uint32(0), None,
                c_uint32(sizeof(c_void_p)), byref(ptr_val),
            )
            time.sleep(0.1)

        # Step 3: Set master (clock source) device via composition
        if device_uids:
            try:
                ptr = c_void_p(0)
                comp_addr = AudioObjectPropertyAddress(
                    fourcc('acom'), kAudioObjectPropertyScopeGlobal,
                    kAudioObjectPropertyElementMain,
                )
                sz = c_uint32(sizeof(c_void_p))
                self._ca.AudioObjectGetPropertyData(
                    c_uint32(agg_id), byref(comp_addr),
                    c_uint32(0), None, byref(sz), byref(ptr),
                )
                if ptr.value:
                    comp = objc.objc_object(c_void_p=ptr.value).mutableCopy()
                    comp.setObject_forKey_(device_uids[0], 'master')
                    comp_ptr = c_void_p(objc.pyobjc_id(comp))
                    self._ca.AudioObjectSetPropertyData(
                        c_uint32(agg_id), byref(comp_addr),
                        c_uint32(0), None,
                        c_uint32(sizeof(c_void_p)), byref(comp_ptr),
                    )
            except Exception:
                pass  # master defaults to first sub-device

        time.sleep(0.2)

        # Step 4: Increase buffer size to reduce audio glitches with Bluetooth
        self._set_buffer_size(agg_id, 4096)
        for uid in device_uids:
            for dev in self.get_all_devices():
                if dev.uid == uid:
                    self._set_buffer_size(dev.device_id, 4096)
                    break

        # Step 5: For stereo mode, configure channel mapping
        if mode == Mode.STEREO and len(device_uids) >= 2:
            self._configure_stereo_channels(agg_id, device_uids)

        # Step 6: Enable drift compensation on non-master sub-devices
        self._enable_drift_compensation(agg_id, device_uids)

        return agg_id

    def _set_buffer_size(self, device_id: int, frames: int):
        """Set the I/O buffer size. Larger = more stable, higher latency."""
        kBufferFrameSize = fourcc('fsiz')
        try:
            val = c_uint32(frames)
            self._set_property(
                device_id, kBufferFrameSize,
                kAudioObjectPropertyScopeGlobal,
                val, sizeof(c_uint32),
            )
        except CoreAudioError:
            pass

    def _configure_stereo_channels(self, agg_device_id: int, device_uids: list[str]):
        """Map left channel to speaker 1, right channel to speaker 2."""
        ch_count = self._get_output_channel_count(device_uids[0])
        left_channel = 1
        right_channel = ch_count + 1
        channels = (c_uint32 * 2)(left_channel, right_channel)
        try:
            self._set_property(
                agg_device_id,
                kAudioDevicePropertyPreferredChannelsForStereo,
                kAudioObjectPropertyScopeOutput,
                channels, sizeof(channels),
            )
        except CoreAudioError:
            pass  # non-fatal: stereo may still work with default mapping

    def _enable_drift_compensation(self, agg_device_id: int, device_uids: list[str]):
        """Enable drift compensation on non-master sub-devices via composition."""
        if len(device_uids) <= 1:
            return
        # Read current composition, update drift on non-master sub-devices,
        # and write it back
        try:
            ptr = c_void_p(0)
            addr = AudioObjectPropertyAddress(
                fourcc('acom'), kAudioObjectPropertyScopeGlobal,
                kAudioObjectPropertyElementMain,
            )
            size = c_uint32(sizeof(c_void_p))
            status = self._ca.AudioObjectGetPropertyData(
                c_uint32(agg_device_id), byref(addr),
                c_uint32(0), None, byref(size), byref(ptr),
            )
            if status != 0 or not ptr.value:
                return
            comp = objc.objc_object(c_void_p=ptr.value)
            comp_mut = comp.mutableCopy()
            subs = comp_mut.objectForKey_('subdevices')
            if subs and subs.count() > 1:
                subs_mut = subs.mutableCopy()
                for i in range(1, subs_mut.count()):
                    sub = subs_mut.objectAtIndex_(i).mutableCopy()
                    sub.setObject_forKey_(NSNumber.numberWithInt_(1), 'drift')
                    subs_mut.replaceObjectAtIndex_withObject_(i, sub)
                comp_mut.setObject_forKey_(subs_mut, 'subdevices')
                comp_ptr = c_void_p(objc.pyobjc_id(comp_mut))
                self._ca.AudioObjectSetPropertyData(
                    c_uint32(agg_device_id), byref(addr),
                    c_uint32(0), None,
                    c_uint32(sizeof(c_void_p)), byref(comp_ptr),
                )
        except Exception:
            pass

    # ── Volume Control ───────────────────────────────────────────────

    def get_volume(self, device_id: int) -> float | None:
        """Get volume (0.0-1.0) for a device. Tries per-channel (element 1)."""
        kVolumeScalar = fourcc('volm')
        # Bluetooth devices use per-channel volume (element 1, 2) not master (0)
        for element in [0, 1]:
            try:
                val = ctypes.c_float(0.0)
                addr = AudioObjectPropertyAddress(
                    kVolumeScalar, kAudioObjectPropertyScopeOutput, element
                )
                size = c_uint32(sizeof(ctypes.c_float))
                status = self._ca.AudioObjectGetPropertyData(
                    c_uint32(device_id), byref(addr),
                    c_uint32(0), None, byref(size), byref(val),
                )
                if status == 0:
                    return val.value
            except Exception:
                continue
        return None

    def set_volume(self, device_id: int, volume: float):
        """Set volume (0.0-1.0) on all channels of a device."""
        kVolumeScalar = fourcc('volm')
        vol = max(0.0, min(1.0, volume))
        # Set on both channels (elements 1 and 2) and master (0)
        for element in [0, 1, 2]:
            try:
                val = ctypes.c_float(vol)
                addr = AudioObjectPropertyAddress(
                    kVolumeScalar, kAudioObjectPropertyScopeOutput, element
                )
                self._ca.AudioObjectSetPropertyData(
                    c_uint32(device_id), byref(addr),
                    c_uint32(0), None,
                    c_uint32(sizeof(ctypes.c_float)), byref(val),
                )
            except Exception:
                continue

    def set_volume_on_sub_devices(self, volume: float):
        """Set volume on all sub-devices of the active aggregate device."""
        if not self._sub_device_ids:
            return
        for dev in self.get_all_devices():
            if dev.uid in self._sub_device_ids:
                try:
                    self.set_volume(dev.device_id, volume)
                except Exception:
                    pass

    def get_sub_device_volume(self) -> float | None:
        """Get volume from the first sub-device (as reference)."""
        if not self._sub_device_ids:
            return None
        for dev in self.get_all_devices():
            if dev.uid in self._sub_device_ids:
                vol = self.get_volume(dev.device_id)
                if vol is not None:
                    return vol
        return None

    def destroy_aggregate_device(self):
        if self._aggregate_device_id is not None:
            try:
                self._ca.AudioHardwareDestroyAggregateDevice(
                    c_uint32(self._aggregate_device_id)
                )
            except Exception:
                pass
            self._aggregate_device_id = None
            self._sub_device_ids = []

    @property
    def is_active(self) -> bool:
        return self._aggregate_device_id is not None

    @property
    def aggregate_device_id(self) -> int | None:
        return self._aggregate_device_id

    # ── Orphan Cleanup ───────────────────────────────────────────────

    def cleanup_orphaned_devices(self):
        """Destroy any aggregate devices left over from a previous crash."""
        for dev in self.get_all_devices():
            if dev.uid.startswith(AGGREGATE_UID_PREFIX):
                try:
                    self._ca.AudioHardwareDestroyAggregateDevice(c_uint32(dev.device_id))
                except Exception:
                    pass

    # ── Device Change Listener ───────────────────────────────────────

    def register_device_change_listener(self, handler):
        self._device_change_handler = handler

        def _callback(obj_id, num_addr, addresses, client_data):
            if self._device_change_handler:
                # Dispatch to main thread via a simple delayed call
                from AppKit import NSObject as _NSObj
                _Dispatcher.instance = handler
                _Dispatcher.alloc().init().performSelectorOnMainThread_withObject_waitUntilDone_(
                    'fire:', None, False
                )
            return 0

        self._listener_callback = AudioObjectPropertyListenerProc(_callback)
        addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDevices,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        self._ca.AudioObjectAddPropertyListener(
            c_uint32(kAudioObjectSystemObject),
            byref(addr),
            self._listener_callback,
            None,
        )

    def unregister_device_change_listener(self):
        if self._listener_callback is not None:
            addr = AudioObjectPropertyAddress(
                kAudioHardwarePropertyDevices,
                kAudioObjectPropertyScopeGlobal,
                kAudioObjectPropertyElementMain,
            )
            self._ca.AudioObjectRemovePropertyListener(
                c_uint32(kAudioObjectSystemObject),
                byref(addr),
                self._listener_callback,
                None,
            )
            self._listener_callback = None

    # ── Cleanup ──────────────────────────────────────────────────────

    def _cleanup(self):
        self.restore_original_default()
        self.destroy_aggregate_device()
        self.unregister_device_change_listener()


# Helper NSObject for dispatching callbacks to the main thread
from AppKit import NSObject as _AppKitNSObject

class _Dispatcher(_AppKitNSObject):
    instance = None

    def fire_(self, _sender):
        if _Dispatcher.instance:
            _Dispatcher.instance()
