# HomePod Streamer

Streams Windows system audio to a HomePod mini over AirPlay (RAOP).

## Setup

```powershell
cd homepod-streamer
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

In the Home app, set **Home Settings > Speakers & TV > Allow Speaker & TV Access = "Everyone on the Same Network"**, with no password.

## Standalone exe

```powershell
pip install -r requirements-dev.txt
python build.py            # -> dist\HomePod Streamer.exe (single file, ~33 MB)
```

Copy `HomePod Streamer.exe` anywhere and run it; Python is not needed on the target machine.

- **First run**: the app shows a firewall warning because the exe is a new program for Windows
  Firewall. Click **Allow** (administrator approval) once. Rules are per program path, so moving
  the exe to another folder needs this again.
- **Settings and log** live in `%APPDATA%\HomePod Streamer\` (`config.json`,
  `homepod-streamer.log`). When running from source they stay in the project folder.
- **ffmpeg** is not bundled. If it's in `PATH`, the "High quality (soxr)" resampler is available;
  otherwise the built-in one is used.
- The exe is not code-signed, so Windows SmartScreen may warn on first launch
  ("More info" > "Run anyway").

### Start with Windows

**Settings > General > Start with Windows** adds a per-user entry to
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (no admin rights) that starts the app
hidden in the tray. Together with **Start streaming when the app opens**, the PC streams to the
last used speaker right after sign-in. If that speaker isn't reachable yet (the network may
still be coming up), the app rescans up to 6 times, 10 s apart; it never switches to a
different speaker on its own. If the exe is moved, the entry is updated on its next launch.

## Interface (GUI)

Double-click the **`HomePod Streamer.lnk`** shortcut in the project folder (no console window opens).
You can copy it to the desktop or the Start menu. Alternative: `python main.py`.

- **Stream**: speaker and audio source selection, Start/Stop, live volume, input level meter
  and connection status
- **Settings**: resampler (Standard / High quality soxr), jitter buffer, latency cap, theme
- **Log**: live log with a "Verbose logging" switch

All choices are saved to `config.json` and remembered on the next launch.

### System tray

The app keeps running in the system tray when you close the window (turn this off under
**Settings > General**). The tray icon shows the status with a colored dot (green: streaming,
orange: connecting/reconnecting, red: error) and its tooltip names the speaker.

- **Left-click**: show the window
- **Right-click**: Start/Stop, Speaker, Audio source, Volume (±5 or 10–100%), Show window, **Exit**

While the window is hidden, Windows notifications report a lost connection, a successful
reconnect and errors. **Exit** in the tray menu is what fully quits the app. Launching the app
again while it is running brings the existing window back instead of opening a second one.
`python main.py --minimized` starts directly in the tray.

## Command line

```powershell
python main.py scan                          # list AirPlay devices on the network
python main.py play samples\tone.wav         # play a file (prompts for a device, saves it to config.json)
python main.py play music.mp3 --device "Living Room" --volume 30
python main.py --debug play samples\tone.wav # verbose pyatv logs
python main.py --host 192.168.1.50 scan      # query an IP directly if mDNS doesn't work

python main.py loopbacks                     # output devices that can be captured
python main.py live                          # stream system audio live (Ctrl+C to stop)
python main.py live --volume 30 --loopback "Headphones"
python main.py live --resampler ffmpeg       # 48k -> 44.1k conversion via ffmpeg/soxr (ffmpeg must be in PATH)
```

Device names come from Windows, so they appear in your Windows display language
(e.g. "Speakers" or "Hoparlör"). `--loopback` matches any part of the name.

### Live streaming options

| Option | Default | Description |
|---|---|---|
| `--loopback` | system default | Part of the name of the output device to capture |
| `--chunk-ms` | 20 | WASAPI capture period |
| `--prebuffer-ms` | 100 | Jitter buffer. Increase if the stats show frequent underruns |
| `--max-buffer-ms` | 300 | If the queue exceeds this it is trimmed to prebuffer level (latency cap) |
| `--resampler` | ffmpeg if found, else miniaudio | `miniaudio` (inside pyatv) or `ffmpeg` (soxr, higher quality) |
| `--stats-interval` | 10 | Stats log interval (s) |

Stats line: `sent` should match real time (~10 s every 10 s). `underruns` should be 0 while
audio is playing. `peak` is the peak level of the captured audio; 0% means nothing is being
captured (wrong output device?).

### Reliability

| Situation | Detection | Behavior |
|---|---|---|
| Network drop, HomePod restarted or powered off | TCP probe to the HomePod's AirPlay port every 2 s; 2 failures in a row | Reconnects automatically (backoff 1, 2, 4 … 30 s; rescans from the 2nd attempt in case the IP changed). Volume is re-applied. |
| Another device (iPhone, Mac…) takes over the HomePod | Control connection closed while the HomePod is still reachable | Stops with a message, **no** reconnect (so the two senders don't fight) |
| Default output changes (e.g. headphones plugged in) | Windows Core Audio poll every 1.5 s | Capture switches to the new device **without interrupting the AirPlay session**. If its sample rate differs, an ffmpeg resampler is inserted. |
| Selected device disappears | Capture stream stops | Falls back to the system default |
| Ctrl+C / Ctrl+Break / closing the window | | Clean shutdown: HomePod session torn down, capture and ffmpeg released |

Source switching only applies when the audio source is **System default**. If you pick a
specific device, the app stays on it even when the Windows default changes.

Automatic reconnect only starts after audio has flowed at least once; if the very first
connection fails (e.g. firewall), the error is shown instead of retrying forever.
`python main.py live --no-reconnect` disables it on the command line.

### Modules

| File | Purpose |
|---|---|
| `discovery.py` | AirPlay device discovery |
| `capture.py` | WASAPI loopback capture, ffmpeg/soxr resampling, default output monitor (pycaw) |
| `streamer.py` | Jitter buffer, endless WAV stream, source switching, `LiveSession` (pyatv) with reconnect, connection watchdog and reachability probe, single-stream lock |
| `controller.py` | Runs the asyncio engine on a background thread, event queue to the UI |
| `gui.py` | CustomTkinter interface |
| `tray.py` | System tray icon and menu (pystray) |
| `firewall.py` | Windows Firewall check and rule creation (UAC) |
| `config.py` | `config.json`, data folder (project folder or `%APPDATA%` for the exe) |
| `autostart.py` | Start with Windows (HKCU Run key) |
| `build.py` | PyInstaller build of the single-file exe |
| `main.py` | Entry point: GUI with no arguments, CLI with subcommands |
| `make_icon.py` | Generates `assets/icon.ico` (one-off) |

### How it works

WASAPI loopback (PyAudioWPatch) → (optional ffmpeg/soxr) → jitter buffer → endless WAV
stream → `pyatv` `stream_file()` (RAOP, 44.1 kHz/16-bit/stereo). Loopback produces no data
while Windows isn't playing anything, so silence is inserted; otherwise pyatv would treat the
stream as finished. Because of the length limit in the WAV header, the stream restarts with a
short gap roughly every 6 hours.

`pyatv` is pinned to 0.18.0: the watchdog that detects a closed HomePod session reads an
internal pyatv field. If it is missing in another version, the watchdog disables itself.

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| "The firewall has no rule for this app" / "The HomePod could not complete setup" | During AirPlay 2 setup the HomePod connects back to this computer. Windows Firewall stores permissions per program path, so the terminal (`python.exe`) may be allowed while the GUI (`pythonw.exe`) is blocked. The app's **Allow** button adds a local-network-only rule ("HomePod Streamer") with administrator approval. |
| "The HomePod closed the session" | Another device (iPhone, Mac…) connected to the HomePod. Streaming stops on purpose; press Start again to take it back. |
| "Reconnecting…" that never succeeds | The HomePod is off or on another network. Press **Reconnecting… (cancel)** to stop. |
| "Another HomePod Streamer stream is already running" | Only one stream can run at a time (GUI or terminal). Close the other one. |
| The level meter doesn't move | Audio is playing through a different output. Select the right device under "Audio source". |

## Known limitations

- **AirPlay has about 2 seconds of latency.** Fine for music and podcasts; **not suitable**
  for video or game audio sync.
- Audio plays from both the PC speakers and the HomePod (loopback copies the playing device).
  Whether the Windows master volume/mute affects the captured signal depends on the driver;
  check the `peak` value in the stats line.
- If Windows Firewall blocks Python's local network access (mDNS UDP 5353), no devices are
  found; allow "Private networks" in the prompt that appears on first run.
