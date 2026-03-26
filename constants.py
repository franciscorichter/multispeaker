"""
Shared constants, ctypes struct definitions, and enums for DualSpeaker.
"""

import ctypes
import struct


def fourcc(s: str) -> int:
    """Convert a 4-character ASCII code to a UInt32."""
    return struct.unpack('>I', s.encode('ascii'))[0]


# --- AudioObject property selectors ---
kAudioHardwarePropertyDevices = fourcc('dev#')
kAudioHardwarePropertyDefaultOutputDevice = fourcc('dOut')
kAudioObjectPropertyName = fourcc('lnam')
kAudioDevicePropertyDeviceUID = fourcc('uid ')
kAudioDevicePropertyStreams = fourcc('stm#')
kAudioDevicePropertyTransportType = fourcc('tran')
kAudioDevicePropertyPreferredChannelsForStereo = fourcc('dch2')
kAudioDevicePropertyStreamConfiguration = fourcc('slay')

# --- Scopes ---
kAudioObjectPropertyScopeGlobal = fourcc('glob')
kAudioObjectPropertyScopeOutput = fourcc('outp')

# --- Transport types ---
kAudioDeviceTransportTypeBluetooth = fourcc('blue')
kAudioDeviceTransportTypeBluetoothLE = fourcc('blea')

# --- Well-known object IDs ---
kAudioObjectSystemObject = 1
kAudioObjectPropertyElementMain = 0

# --- Aggregate device UID prefix ---
AGGREGATE_UID_PREFIX = 'com.multispeaker.'

# --- ctypes struct for AudioObjectPropertyAddress ---
class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ('mSelector', ctypes.c_uint32),
        ('mScope', ctypes.c_uint32),
        ('mElement', ctypes.c_uint32),
    ]


# --- Callback type for property listeners ---
AudioObjectPropertyListenerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32,   # OSStatus return
    ctypes.c_uint32,  # AudioObjectID
    ctypes.c_uint32,  # inNumberAddresses
    ctypes.c_void_p,  # const AudioObjectPropertyAddress*
    ctypes.c_void_p,  # void* clientData
)


class Mode:
    PARTY = 'party'    # multi-output, stacked=True, same audio to both
    STEREO = 'stereo'  # aggregate, stacked=False, L/R split
