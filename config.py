"""
Persistence for DualSpeaker preferences via NSUserDefaults.
"""

from Foundation import NSUserDefaults
from constants import Mode


class Config:
    _SUITE = 'com.multispeaker.preferences'
    _KEY_UIDS = 'selectedDeviceUIDs'
    _KEY_MODE = 'mode'

    def __init__(self):
        self._defaults = NSUserDefaults.alloc().initWithSuiteName_(self._SUITE)

    @property
    def selected_device_uids(self) -> list[str]:
        arr = self._defaults.arrayForKey_(self._KEY_UIDS)
        return list(arr) if arr else []

    @selected_device_uids.setter
    def selected_device_uids(self, uids: list[str]):
        self._defaults.setObject_forKey_(uids, self._KEY_UIDS)

    @property
    def mode(self) -> str:
        val = self._defaults.stringForKey_(self._KEY_MODE)
        return val if val in (Mode.PARTY, Mode.STEREO) else Mode.PARTY

    @mode.setter
    def mode(self, value: str):
        self._defaults.setObject_forKey_(value, self._KEY_MODE)
