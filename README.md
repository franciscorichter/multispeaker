# MultiSpeaker

A macOS menu bar app that combines multiple Bluetooth speakers into a single audio output. Supports **Party Mode** (same audio to all speakers) and **Stereo Mode** (left/right split across speakers).

## Features

- Automatically detects connected Bluetooth speakers
- Party Mode: plays the same audio through all selected speakers
- Stereo Mode: routes left channel to one speaker and right channel to another
- Volume key interception: hardware volume keys control all speakers simultaneously
- Menu bar controls for volume, mode selection, and activation
- Remembers your speaker selection and mode preference between sessions
- Cleans up gracefully on quit or crash

## Requirements

- macOS
- Python 3.10+

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 app.py
```

1. Click the speaker icon in the menu bar
2. Select two or more Bluetooth speakers
3. Choose a mode:
   - **Party Mode** — same audio to all speakers
   - **Stereo Mode** — left/right split across two speakers
4. Click **Activate**

To stop, click the menu bar icon and select **Deactivate**.

### Volume key support

For hardware volume keys to control your speakers directly, grant Accessibility permissions to your terminal (System Settings > Privacy & Security > Accessibility). Without this, volume controls in the menu still work.

## How it works

MultiSpeaker creates a macOS aggregate audio device from your selected Bluetooth speakers using CoreAudio, sets it as the default output, and intercepts volume key events to keep all speakers in sync.
