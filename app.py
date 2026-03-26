"""
MultiSpeaker — macOS menu bar app to combine two Bluetooth speakers.
Supports Party Mode (same audio) and Stereo Mode (L/R split).
"""

import signal
import objc
from AppKit import (
    NSApplication, NSStatusBar, NSMenu, NSMenuItem, NSImage,
    NSVariableStatusItemLength, NSObject, NSAlert,
    NSApplicationActivationPolicyProhibited,
    NSOnState, NSOffState, NSEvent,
)
from Foundation import NSTimer
from Quartz import (
    CGEventTapCreate, CGEventTapEnable,
    kCGSessionEventTap, kCGHeadInsertEventTap,
    kCGEventTapOptionDefault,
    CGEventMaskBit,
    CFMachPortCreateRunLoopSource, CFMachPortInvalidate,
    CFRunLoopGetCurrent, CFRunLoopAddSource, CFRunLoopRemoveSource,
    kCFRunLoopCommonModes,
)

from core_audio import CoreAudioManager, CoreAudioError
from config import Config
from constants import Mode

# Media key constants
NX_SYSDEFINED = 14
NX_SUBTYPE_AUX_CONTROL_BUTTON = 8
NX_KEYTYPE_SOUND_UP = 0
NX_KEYTYPE_SOUND_DOWN = 1
NX_KEYTYPE_MUTE = 7
VOLUME_STEP = 0.0625  # 1/16, same step size as macOS


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notification):
        self._manager = CoreAudioManager()
        self._config = Config()
        self._selected_uids: set[str] = set(self._config.selected_device_uids)
        self._event_tap = None
        self._tap_source = None
        self._muted_volume = 0.5
        self._ignore_device_changes = False
        self._debounce_timer = None

        # Clean up any orphaned aggregate devices from a previous crash
        self._manager.cleanup_orphaned_devices()

        # Create status bar item
        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._set_icon(active=False)
        self._status_item.setHighlightMode_(True)

        # Build menu
        self._rebuild_menu()

        # Listen for device changes (BT connect/disconnect)
        self._manager.register_device_change_listener(self._on_devices_changed)

    def _set_icon(self, active: bool):
        symbol_name = 'speaker.wave.2.fill' if active else 'speaker.wave.2'
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol_name, 'MultiSpeaker'
        )
        if image:
            image.setTemplate_(True)
            self._status_item.button().setImage_(image)
        else:
            self._status_item.setTitle_('DS' if not active else 'DS*')

    # ── Volume Key Interception (CGEventTap) ─────────────────────────

    def _start_volume_tap(self):
        """Intercept keyboard volume keys via CGEventTap and forward to speakers."""
        if self._event_tap is not None:
            return

        # Store reference to self for the callback closure
        delegate = self

        def tap_callback(proxy, event_type, event, refcon):
            # If tap gets disabled by macOS (timeout), re-enable it
            if event_type == 0xFFFFFFFE:  # kCGEventTapDisabledByTimeout
                if delegate._event_tap:
                    CGEventTapEnable(delegate._event_tap, True)
                return event

            if event_type != NX_SYSDEFINED:
                return event

            if not delegate._manager.is_active:
                return event

            # Convert CGEvent to NSEvent to read media key data
            try:
                ns_event = NSEvent.eventWithCGEvent_(event)
                if ns_event is None or ns_event.subtype() != NX_SUBTYPE_AUX_CONTROL_BUTTON:
                    return event

                data = ns_event.data1()
                key_code = (data & 0xFFFF0000) >> 16
                key_state = (data & 0x0000FF00) >> 8

                # Only handle volume keys
                if key_code not in (NX_KEYTYPE_SOUND_UP, NX_KEYTYPE_SOUND_DOWN, NX_KEYTYPE_MUTE):
                    return event

                # Only act on key down and key repeat
                if key_state not in (0x0A, 0x0B):
                    return event

                vol = delegate._manager.get_sub_device_volume()
                if vol is None:
                    vol = 0.5

                if key_code == NX_KEYTYPE_SOUND_UP:
                    delegate._manager.set_volume_on_sub_devices(min(1.0, vol + VOLUME_STEP))
                elif key_code == NX_KEYTYPE_SOUND_DOWN:
                    delegate._manager.set_volume_on_sub_devices(max(0.0, vol - VOLUME_STEP))
                elif key_code == NX_KEYTYPE_MUTE:
                    if vol > 0.001:
                        delegate._muted_volume = vol
                        delegate._manager.set_volume_on_sub_devices(0.0)
                    else:
                        delegate._manager.set_volume_on_sub_devices(delegate._muted_volume)

                # Block the event so macOS doesn't show the "disabled volume" HUD
                return None

            except Exception:
                return event

        self._event_tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionDefault,  # active tap: can block events
            CGEventMaskBit(NX_SYSDEFINED),
            tap_callback,
            None,
        )

        if self._event_tap is None:
            # Fallback: no accessibility permission, use observe-only
            self._start_volume_monitor_fallback()
            return

        self._tap_source = CFMachPortCreateRunLoopSource(None, self._event_tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), self._tap_source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._event_tap, True)

    def _stop_volume_tap(self):
        if self._event_tap is not None:
            CGEventTapEnable(self._event_tap, False)
            if self._tap_source is not None:
                CFRunLoopRemoveSource(CFRunLoopGetCurrent(), self._tap_source, kCFRunLoopCommonModes)
                self._tap_source = None
            CFMachPortInvalidate(self._event_tap)
            self._event_tap = None
        self._stop_volume_monitor_fallback()

    # Fallback: NSEvent global monitor (observe-only, can't block HUD)
    _fallback_monitor = None

    def _start_volume_monitor_fallback(self):
        if self._fallback_monitor is not None:
            return
        delegate = self

        def handler(event):
            if event.subtype() != NX_SUBTYPE_AUX_CONTROL_BUTTON:
                return
            if not delegate._manager.is_active:
                return
            data = event.data1()
            key_code = (data & 0xFFFF0000) >> 16
            key_state = (data & 0x0000FF00) >> 8
            if key_state not in (0x0A, 0x0B):
                return
            vol = delegate._manager.get_sub_device_volume() or 0.5
            if key_code == NX_KEYTYPE_SOUND_UP:
                delegate._manager.set_volume_on_sub_devices(min(1.0, vol + VOLUME_STEP))
            elif key_code == NX_KEYTYPE_SOUND_DOWN:
                delegate._manager.set_volume_on_sub_devices(max(0.0, vol - VOLUME_STEP))

        self._fallback_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            1 << 14, handler
        )

    def _stop_volume_monitor_fallback(self):
        if self._fallback_monitor is not None:
            NSEvent.removeMonitor_(self._fallback_monitor)
            self._fallback_monitor = None

    # ── Menu ─────────────────────────────────────────────────────────

    def _rebuild_menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        # Title
        title = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'MultiSpeaker', None, ''
        )
        title.setEnabled_(False)
        menu.addItem_(title)
        menu.addItem_(NSMenuItem.separatorItem())

        # Bluetooth devices
        bt_devices = self._manager.get_bluetooth_output_devices()
        is_active = self._manager.is_active

        if not bt_devices:
            no_dev = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                'No Bluetooth speakers found', None, ''
            )
            no_dev.setEnabled_(False)
            menu.addItem_(no_dev)
        else:
            available_uids = {d.uid for d in bt_devices}
            self._selected_uids &= available_uids

            for dev in bt_devices:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    dev.name, 'toggleDevice:', ''
                )
                item.setTarget_(self)
                item.setRepresentedObject_(dev.uid)
                item.setState_(NSOnState if dev.uid in self._selected_uids else NSOffState)
                item.setEnabled_(not is_active)
                menu.addItem_(item)

        menu.addItem_(NSMenuItem.separatorItem())

        # Mode selection
        current_mode = self._config.mode

        party_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Party Mode (same audio)', 'selectMode:', ''
        )
        party_item.setTarget_(self)
        party_item.setRepresentedObject_(Mode.PARTY)
        party_item.setState_(NSOnState if current_mode == Mode.PARTY else NSOffState)
        party_item.setEnabled_(not is_active)
        menu.addItem_(party_item)

        stereo_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Stereo Mode (L/R split)', 'selectMode:', ''
        )
        stereo_item.setTarget_(self)
        stereo_item.setRepresentedObject_(Mode.STEREO)
        stereo_item.setState_(NSOnState if current_mode == Mode.STEREO else NSOffState)
        stereo_item.setEnabled_(not is_active)
        menu.addItem_(stereo_item)

        menu.addItem_(NSMenuItem.separatorItem())

        # Activate / Deactivate
        act_title = 'Deactivate' if is_active else 'Activate'
        act_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            act_title, 'toggleActivation:', ''
        )
        act_item.setTarget_(self)
        can_activate = len(self._selected_uids) >= 2 or is_active
        act_item.setEnabled_(can_activate)
        menu.addItem_(act_item)

        if not is_active and len(self._selected_uids) < 2:
            hint = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                '  Select at least 2 speakers', None, ''
            )
            hint.setEnabled_(False)
            menu.addItem_(hint)

        # Volume controls (only when active)
        if is_active:
            menu.addItem_(NSMenuItem.separatorItem())

            vol = self._manager.get_sub_device_volume()
            vol_pct = int((vol or 0.5) * 100)
            vol_label = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f'Volume: {vol_pct}%', None, ''
            )
            vol_label.setEnabled_(False)
            menu.addItem_(vol_label)

            vol_up = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                'Volume Up (+10%)', 'volumeUp:', '+'
            )
            vol_up.setTarget_(self)
            menu.addItem_(vol_up)

            vol_down = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                'Volume Down (-10%)', 'volumeDown:', '-'
            )
            vol_down.setTarget_(self)
            menu.addItem_(vol_down)

        menu.addItem_(NSMenuItem.separatorItem())

        # Refresh
        refresh_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Refresh Devices', 'refreshDevices:', ''
        )
        refresh_item.setTarget_(self)
        menu.addItem_(refresh_item)

        # Quit
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Quit', 'quit:', 'q'
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    # ── Actions ──────────────────────────────────────────────────────

    @objc.typedSelector(b'v@:@')
    def toggleDevice_(self, sender):
        uid = sender.representedObject()
        if uid in self._selected_uids:
            self._selected_uids.discard(uid)
        else:
            self._selected_uids.add(uid)
        self._config.selected_device_uids = list(self._selected_uids)
        self._rebuild_menu()

    @objc.typedSelector(b'v@:@')
    def selectMode_(self, sender):
        mode = sender.representedObject()
        self._config.mode = mode
        self._rebuild_menu()

    @objc.typedSelector(b'v@:@')
    def toggleActivation_(self, sender):
        if self._manager.is_active:
            self._deactivate()
        else:
            self._activate()

    @objc.typedSelector(b'v@:@')
    def volumeUp_(self, sender):
        vol = self._manager.get_sub_device_volume() or 0.5
        self._manager.set_volume_on_sub_devices(min(1.0, vol + 0.1))
        self._rebuild_menu()

    @objc.typedSelector(b'v@:@')
    def volumeDown_(self, sender):
        vol = self._manager.get_sub_device_volume() or 0.5
        self._manager.set_volume_on_sub_devices(max(0.0, vol - 0.1))
        self._rebuild_menu()

    @objc.typedSelector(b'v@:@')
    def refreshDevices_(self, sender):
        self._rebuild_menu()

    @objc.typedSelector(b'v@:@')
    def quit_(self, sender):
        if self._manager.is_active:
            self._deactivate()
        self._stop_volume_tap()
        self._manager.unregister_device_change_listener()
        NSApplication.sharedApplication().terminate_(None)

    # ── Activate / Deactivate ────────────────────────────────────────

    def _activate(self):
        uids = list(self._selected_uids)
        if len(uids) < 2:
            return
        mode = self._config.mode
        mode_name = 'Party Mode' if mode == Mode.PARTY else 'Stereo Mode'
        self._ignore_device_changes = True
        try:
            self._manager.save_original_default()
            agg_id = self._manager.create_aggregate_device(
                uids, mode, name=f'MultiSpeaker ({mode_name})'
            )
            self._manager.set_default_output_device(agg_id)
            self._set_icon(active=True)
            self._start_volume_tap()
            self._rebuild_menu()
        except CoreAudioError as e:
            self._manager.restore_original_default()
            self._manager.destroy_aggregate_device()
            self._show_error('Activation Failed', str(e))
        finally:
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                3.0, self, 'enableDeviceChanges:', None, False
            )

    @objc.typedSelector(b'v@:@')
    def enableDeviceChanges_(self, timer):
        self._ignore_device_changes = False

    def _deactivate(self):
        self._ignore_device_changes = True
        self._stop_volume_tap()
        self._manager.restore_original_default()
        self._manager.destroy_aggregate_device()
        self._set_icon(active=False)
        self._ignore_device_changes = False
        self._rebuild_menu()

    # ── Device Change Handler ────────────────────────────────────────

    def _on_devices_changed(self):
        if self._ignore_device_changes:
            return
        if self._debounce_timer is not None:
            self._debounce_timer.invalidate()
        self._debounce_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, 'debouncedDeviceCheck:', None, False
        )

    @objc.typedSelector(b'v@:@')
    def debouncedDeviceCheck_(self, timer):
        self._debounce_timer = None
        if self._ignore_device_changes:
            return
        if self._manager.is_active:
            bt_devices = self._manager.get_bluetooth_output_devices()
            current_uids = {d.uid for d in bt_devices}
            missing = self._selected_uids - current_uids
            if missing:
                self._deactivate()
        self._rebuild_menu()

    # ── Error Dialog ─────────────────────────────────────────────────

    def _show_error(self, title: str, message: str):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    signal.signal(signal.SIGTERM, lambda *_: app.terminate_(None))

    app.run()


if __name__ == '__main__':
    main()
