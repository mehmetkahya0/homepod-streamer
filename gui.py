"""CustomTkinter desktop interface."""

from __future__ import annotations

import ctypes
import logging
import math
import queue
import shutil
import subprocess
import sys
import time
import tkinter.font as tkfont
from pathlib import Path

import customtkinter as ctk

from capture import display_name
from config import load_config, update_config
from controller import ACTIVE_STATES, Controller, Event, State
from discovery import AirPlayDevice
from firewall import FirewallState
from streamer import RAOP_RATE, LiveSettings, acquire_named_mutex

_LOGGER = logging.getLogger(__name__)

APP_NAME = "HomePod Streamer"
ICON_PATH = Path(__file__).with_name("assets") / "icon.ico"
DEFAULT_SOURCE = "System default"

# (light, dark) color pairs, inspired by Apple system colors
C = {
    "bg": ("#F2F2F7", "#161618"),
    "card": ("#FFFFFF", "#232326"),
    "border": ("#E3E3E8", "#2F2F33"),
    "text": ("#1C1C1E", "#F2F2F7"),
    "muted": ("#6E6E73", "#98989F"),
    "field": ("#F2F2F7", "#2C2C2F"),
    "field_hover": ("#E5E5EA", "#3A3A3D"),
    "track": ("#E5E5EA", "#3A3A3C"),
    "accent": ("#007AFF", "#0A84FF"),
    "accent_hover": ("#0066D6", "#3395FF"),
    "danger": ("#FF3B30", "#FF453A"),
    "danger_hover": ("#E0352B", "#FF6B62"),
    "ok": ("#28A745", "#30D158"),
    "warn": ("#E08600", "#FF9F0A"),
}

# Segoe Fluent Icons (Win11) / Segoe MDL2 Assets (Win10) code points
GLYPH = {
    "refresh": "", "speaker": "", "source": "", "warning": "",
    "vol0": "", "vol1": "", "vol2": "", "vol3": "", "mute": "",
    "settings": "", "link": "",
}

STATE_STYLE = {
    State.IDLE: ("Ready", "muted"),
    State.CONNECTING: ("Connecting", "warn"),
    State.STREAMING: ("Streaming", "ok"),
    State.RECONNECTING: ("Reconnecting", "warn"),
    State.STOPPING: ("Stopping", "warn"),
    State.ERROR: ("Error", "danger"),
}


def _pick_font(*families: str) -> str:
    available = set(tkfont.families())
    return next((f for f in families if f in available), "TkDefaultFont")


class QueueLogHandler(logging.Handler):
    """Put log records on a queue for the UI thread to read."""

    def __init__(self, q: queue.Queue[str]) -> None:
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


class Card(ctk.CTkFrame):
    """Content card with a title and border."""

    def __init__(self, master, title: str, glyph: str, fonts: dict, **kw) -> None:
        super().__init__(master, fg_color=C["card"], corner_radius=14, border_width=1, border_color=C["border"], **kw)
        self.header = ctk.CTkFrame(self, fg_color="transparent")
        self.header.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(self.header, text=glyph, font=fonts["icon_sm"], text_color=C["accent"], width=18).pack(side="left")
        ctk.CTkLabel(self.header, text=title.upper(), font=fonts["caption_bold"], text_color=C["muted"]).pack(
            side="left", padx=(8, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x", padx=16, pady=(0, 12))


class App(ctk.CTk):
    """Main window."""

    def __init__(self, controller: Controller, log_queue: queue.Queue[str]) -> None:
        super().__init__(fg_color=C["bg"])
        self.controller = controller
        self.log_queue = log_queue
        self.cfg = load_config()
        self.devices: dict[str, AirPlayDevice] = {}
        self.loopbacks: dict[str, dict] = {}
        self._default_loopback: dict | None = None
        self._meter = 0.0
        self._meter_color: tuple | None = None
        self._base_message = ""
        self._vol_pending: float | None = None
        self._vol_touched = 0.0
        self._has_ffmpeg = shutil.which("ffmpeg") is not None

        self.title(APP_NAME)
        self._base_height = 660
        self.geometry(f"460x{self._base_height}")
        self.minsize(420, 620)
        if ICON_PATH.exists():
            self.iconbitmap(default=str(ICON_PATH))

        ui = _pick_font("Segoe UI Variable Text", "Segoe UI")
        display = _pick_font("Segoe UI Variable Display", "Segoe UI")
        icons = _pick_font("Segoe Fluent Icons", "Segoe MDL2 Assets")
        mono = _pick_font("Cascadia Mono", "Consolas")
        self.fonts = {
            "title": ctk.CTkFont(display, 22, "bold"),
            "body": ctk.CTkFont(ui, 13),
            "body_bold": ctk.CTkFont(ui, 13, "bold"),
            "small": ctk.CTkFont(ui, 12),
            "caption_bold": ctk.CTkFont(ui, 11, "bold"),
            "button": ctk.CTkFont(display, 15, "bold"),
            "icon": ctk.CTkFont(icons, 16),
            "icon_sm": ctk.CTkFont(icons, 13),
            "mono": ctk.CTkFont(mono, 11),
        }

        self._build_header()
        self.tabs = ctk.CTkTabview(
            self, fg_color="transparent", corner_radius=10, anchor="w",
            segmented_button_fg_color=C["card"], segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["accent_hover"],
            segmented_button_unselected_color=C["card"], segmented_button_unselected_hover_color=C["field_hover"],
            text_color=C["text"],
        )
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self._build_stream_tab(self.tabs.add("Stream"))
        self._build_settings_tab(self.tabs.add("Settings"))
        self._build_log_tab(self.tabs.add("Log"))
        self.tabs._segmented_button.configure(font=self.fonts["body"])

        self._apply_state(State.IDLE, "")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._tick)
        self.refresh_devices()
        self.refresh_loopbacks()
        self.controller.check_firewall()

    # ------------------------------------------------------------------ layout
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(18, 4))
        ctk.CTkLabel(head, text=APP_NAME, font=self.fonts["title"], text_color=C["text"]).pack(side="left")

        self.pill = ctk.CTkFrame(head, fg_color=C["card"], border_width=1, border_color=C["border"], corner_radius=13, height=26)
        self.pill.pack(side="right")
        self.pill_dot = ctk.CTkLabel(self.pill, text="●", font=self.fonts["small"], width=10)
        self.pill_dot.pack(side="left", padx=(12, 4), pady=2)
        self.pill_text = ctk.CTkLabel(self.pill, text="", font=self.fonts["caption_bold"], text_color=C["text"])
        self.pill_text.pack(side="left", padx=(0, 12), pady=2)

    def _dropdown(self, master, command) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            master, values=["…"], command=command, height=36, corner_radius=10, dynamic_resizing=False,
            fg_color=C["field"], button_color=C["field"], button_hover_color=C["field_hover"],
            text_color=C["text"], dropdown_fg_color=C["card"], dropdown_hover_color=C["field_hover"],
            dropdown_text_color=C["text"], font=self.fonts["body"], dropdown_font=self.fonts["body"],
        )

    def _icon_button(self, master, glyph: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(master, text=glyph, width=36, height=36, corner_radius=10, font=self.fonts["icon"],
                             fg_color=C["field"], hover_color=C["field_hover"], text_color=C["text"], command=command)

    def _build_firewall_banner(self, tab: ctk.CTkFrame) -> None:
        """Firewall block warning; only shown when there is a problem."""
        self.fw_banner = ctk.CTkFrame(tab, fg_color=C["card"], corner_radius=14, border_width=1,
                                      border_color=C["warn"])
        inner = ctk.CTkFrame(self.fw_banner, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        ctk.CTkLabel(inner, text=GLYPH["warning"], font=self.fonts["icon"], text_color=C["warn"],
                     width=20).pack(side="left", anchor="n", pady=(2, 0))
        self.fw_text = ctk.CTkLabel(inner, text="", font=self.fonts["small"], text_color=C["text"],
                                    anchor="w", justify="left", wraplength=250)
        self.fw_text.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.fw_button = ctk.CTkButton(inner, text="Allow", width=84, height=32, corner_radius=8,
                                       font=self.fonts["body_bold"], fg_color=C["accent"],
                                       hover_color=C["accent_hover"], command=self._on_allow_firewall)
        self.fw_button.pack(side="right")

    def _on_allow_firewall(self) -> None:
        self.fw_button.configure(state="disabled", text="Waiting…")
        self.fw_text.configure(text="Windows is asking for administrator approval (UAC). Choose 'Yes' in the dialog.")
        self.controller.allow_firewall()

    def _on_firewall(self, state: FirewallState, message: str) -> None:
        self.fw_button.configure(state="normal", text="Allow")
        if state in (FirewallState.ALLOWED, FirewallState.UNKNOWN):
            if self.fw_banner.winfo_ismapped():
                self.fw_banner.pack_forget()
                self.geometry(f"460x{self._base_height}")
                if not message:
                    self.message.configure(text="Firewall access granted ✓", text_color=C["ok"])
            return
        text = ("The firewall is blocking the HomePod from connecting to this app; streaming can't start."
                if state == FirewallState.BLOCKED else
                "The firewall has no rule for this app, so the HomePod can't connect.")
        if message:
            text += f"\n{message}"
        self.fw_text.configure(text=text)
        if not self.fw_banner.winfo_ismapped():
            self.fw_banner.pack(fill="x", pady=(4, 10), before=self.speaker_card)
            self.geometry(f"460x{self._base_height + 84}")

    def _build_stream_tab(self, tab: ctk.CTkFrame) -> None:
        tab.configure(fg_color="transparent")
        self._build_firewall_banner(tab)

        # Speaker
        card = Card(tab, "Speaker", GLYPH["speaker"], self.fonts)
        card.pack(fill="x", pady=(4, 10))
        self.speaker_card = card
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        self.device_menu = self._dropdown(row, self._on_device_selected)
        self.device_menu.pack(side="left", fill="x", expand=True)
        self.device_refresh = self._icon_button(row, GLYPH["refresh"], self.refresh_devices)
        self.device_refresh.pack(side="left", padx=(8, 0))
        self.device_info = ctk.CTkLabel(card.body, text="", font=self.fonts["small"], text_color=C["muted"], anchor="w")
        self.device_info.pack(fill="x", pady=(6, 0))

        # Audio source
        card = Card(tab, "Audio source", GLYPH["source"], self.fonts)
        card.pack(fill="x", pady=(0, 10))
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        self.source_menu = self._dropdown(row, self._on_source_selected)
        self.source_menu.pack(side="left", fill="x", expand=True)
        self.source_refresh = self._icon_button(row, GLYPH["refresh"], self.refresh_loopbacks)
        self.source_refresh.pack(side="left", padx=(8, 0))
        self.source_info = ctk.CTkLabel(card.body, text="", font=self.fonts["small"], text_color=C["muted"],
                                        anchor="w", justify="left")
        self.source_info.pack(fill="x", pady=(6, 0))
        self.tip = ctk.CTkFrame(card.body, fg_color="transparent")
        ctk.CTkLabel(self.tip, text=GLYPH["warning"], font=self.fonts["icon_sm"], text_color=C["warn"],
                     width=16).pack(side="left", anchor="n", pady=(2, 0))
        ctk.CTkLabel(self.tip, text="No conversion needed at 44100 Hz", font=self.fonts["small"],
                     text_color=C["muted"], anchor="w").pack(side="left", padx=(6, 0))
        link = ctk.CTkLabel(self.tip, text="Change ↗", font=self.fonts["small"], text_color=C["accent"],
                            cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: subprocess.Popen(["control", "mmsys.cpl"]))

        # Start / Stop
        self.main_button = ctk.CTkButton(tab, text="", height=48, corner_radius=24, font=self.fonts["button"],
                                         command=self._on_main_button)
        self.main_button.pack(fill="x", pady=(2, 4))
        self.message = ctk.CTkLabel(tab, text="", font=self.fonts["small"], text_color=C["muted"],
                                    wraplength=390, justify="center")
        self.message.pack(fill="x", pady=(0, 6))

        # Volume
        card = Card(tab, "Volume", GLYPH["vol3"], self.fonts)
        card.pack(fill="x")
        self.vol_value = ctk.CTkLabel(card.header, text="", font=self.fonts["body_bold"], text_color=C["text"])
        self.vol_value.pack(side="right")
        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x")
        self.vol_icon = ctk.CTkLabel(row, text=GLYPH["vol2"], font=self.fonts["icon"], text_color=C["muted"], width=24)
        self.vol_icon.pack(side="left")
        self.vol_slider = ctk.CTkSlider(row, from_=0, to=100, number_of_steps=100, height=18,
                                        progress_color=C["accent"], button_color=C["accent"],
                                        button_hover_color=C["accent_hover"], fg_color=C["track"],
                                        command=self._on_volume)
        self.vol_slider.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.vol_slider.set(float(self.cfg["volume"]))
        self._update_volume_label(float(self.cfg["volume"]))

        row = ctk.CTkFrame(card.body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(row, text="Input", font=self.fonts["small"], text_color=C["muted"], width=24,
                     anchor="w").pack(side="left")
        self.meter = ctk.CTkProgressBar(row, height=6, corner_radius=3, fg_color=C["track"], progress_color=C["ok"])
        self.meter.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.meter.set(0)

    def _setting_block(self, master, title: str, desc: str) -> ctk.CTkFrame:
        block = ctk.CTkFrame(master, fg_color="transparent")
        block.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(block, text=title, font=self.fonts["body_bold"], text_color=C["text"], anchor="w").pack(fill="x")
        ctk.CTkLabel(block, text=desc, font=self.fonts["small"], text_color=C["muted"], anchor="w",
                     justify="left", wraplength=360).pack(fill="x", pady=(2, 8))
        return block

    def _segmented(self, master, values: list[str], command) -> ctk.CTkSegmentedButton:
        return ctk.CTkSegmentedButton(
            master, values=values, command=command, height=32, corner_radius=8, font=self.fonts["small"],
            fg_color=C["field"], selected_color=C["accent"], selected_hover_color=C["accent_hover"],
            unselected_color=C["field"], unselected_hover_color=C["field_hover"], text_color=C["text"],
        )

    def _build_settings_tab(self, tab: ctk.CTkFrame) -> None:
        tab.configure(fg_color="transparent")
        card = Card(tab, "Audio", GLYPH["settings"], self.fonts)
        card.pack(fill="x", pady=(4, 10))

        desc = "If Windows runs at 48 kHz, audio is converted to 44.1 kHz for AirPlay."
        if not self._has_ffmpeg:
            desc += " High quality requires ffmpeg (not found in PATH)."
        block = self._setting_block(card.body, "Resampler", desc)
        self._resampler_labels = {"miniaudio": "Standard", "ffmpeg": "High quality (soxr)"}
        self.resampler_seg = self._segmented(block, list(self._resampler_labels.values()), self._on_resampler)
        self.resampler_seg.pack(fill="x")
        current = self.cfg["resampler"] if self._has_ffmpeg else "miniaudio"
        self.resampler_seg.set(self._resampler_labels[current])
        if not self._has_ffmpeg:
            self.resampler_seg.configure(state="disabled")

        block = self._setting_block(card.body, "Jitter buffer",
                                    "Increase if you hear dropouts or crackling. Adds to latency.")
        self.prebuffer_slider, self.prebuffer_value = self._value_slider(
            block, 40, 400, 18, self.cfg["prebuffer_ms"], "prebuffer_ms")

        block = self._setting_block(card.body, "Latency cap",
                                    "If the buffer grows beyond this, the excess is dropped to keep latency low.")
        self.maxbuf_slider, self.maxbuf_value = self._value_slider(
            block, 200, 1000, 16, self.cfg["max_buffer_ms"], "max_buffer_ms")

        card = Card(tab, "Appearance", GLYPH["settings"], self.fonts)
        card.pack(fill="x")
        self._theme_labels = {"system": "System", "light": "Light", "dark": "Dark"}
        seg = self._segmented(card.body, list(self._theme_labels.values()), self._on_theme)
        seg.pack(fill="x")
        seg.set(self._theme_labels.get(self.cfg["theme"], "System"))

        self.settings_note = ctk.CTkLabel(tab, text="", font=self.fonts["small"], text_color=C["warn"])
        self.settings_note.pack(fill="x", pady=(10, 0))

    def _value_slider(self, master, lo: int, hi: int, steps: int, value: int, key: str):
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x")
        label = ctk.CTkLabel(row, text=f"{int(value)} ms", font=self.fonts["body_bold"], width=64, anchor="e",
                             text_color=C["text"])

        def changed(v: float) -> None:
            label.configure(text=f"{int(v)} ms")
            update_config(**{key: int(v)})
            self.cfg[key] = int(v)
            self._settings_changed()

        slider = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, height=18, command=changed,
                               progress_color=C["accent"], button_color=C["accent"],
                               button_hover_color=C["accent_hover"], fg_color=C["track"])
        slider.pack(side="left", fill="x", expand=True)
        label.pack(side="left", padx=(8, 0))
        slider.set(value)
        return slider, label

    def _build_log_tab(self, tab: ctk.CTkFrame) -> None:
        tab.configure(fg_color="transparent")
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(4, 8))
        self.debug_switch = ctk.CTkSwitch(bar, text="Verbose logging", font=self.fonts["small"],
                                          progress_color=C["accent"], text_color=C["text"],
                                          command=self._on_debug)
        self.debug_switch.pack(side="left")
        if self.cfg["debug"]:
            self.debug_switch.select()
        ctk.CTkButton(bar, text="Clear", width=80, height=30, corner_radius=8, font=self.fonts["small"],
                      fg_color=C["field"], hover_color=C["field_hover"], text_color=C["text"],
                      command=lambda: self.log_box.delete("1.0", "end")).pack(side="right")
        self.log_box = ctk.CTkTextbox(tab, font=self.fonts["mono"], corner_radius=10, fg_color=C["card"],
                                      border_width=1, border_color=C["border"], text_color=C["text"], wrap="word")
        self.log_box.pack(fill="both", expand=True)

    # ----------------------------------------------------------------- actions
    def refresh_devices(self) -> None:
        self.device_menu.configure(values=["Searching…"], state="disabled")
        self.device_menu.set("Searching…")
        self.device_refresh.configure(state="disabled")
        self.device_info.configure(text="Scanning the network for AirPlay speakers…")
        self._update_main_button()
        self.controller.refresh_devices()

    def refresh_loopbacks(self) -> None:
        self.controller.refresh_loopbacks()

    def _selected_device(self) -> AirPlayDevice | None:
        return self.devices.get(self.device_menu.get())

    def _on_device_selected(self, name: str) -> None:
        dev = self.devices.get(name)
        if dev:
            update_config(last_device=dev.identifier)
            self.device_info.configure(text=f"{dev.model} · {dev.address}")
        self._update_main_button()

    def _on_source_selected(self, name: str) -> None:
        update_config(loopback=None if name == DEFAULT_SOURCE else self.loopbacks[name]["name"])
        self._update_source_info()

    def _current_loopback(self) -> dict | None:
        name = self.source_menu.get()
        return self._default_loopback if name == DEFAULT_SOURCE else self.loopbacks.get(name)

    def _update_source_info(self, running_desc: str | None = None) -> None:
        dev = self._current_loopback()
        if running_desc:
            self.source_info.configure(text=running_desc.replace(" [Loopback]", "").replace(" · ", "\n", 1))
        elif dev:
            rate = int(dev["defaultSampleRate"])
            prefix = f"{display_name(dev)}\n" if self.source_menu.get() == DEFAULT_SOURCE else ""
            if rate == RAOP_RATE:
                conv = "no conversion ✓"
            else:
                conv = "via soxr" if self._resampler_key() == "ffmpeg" else "standard conversion"
                conv = f"44.1 kHz ({conv})"
            self.source_info.configure(text=f"{prefix}{rate / 1000:g} kHz · {dev['maxInputChannels']} ch → {conv}")
        else:
            self.source_info.configure(text="")
        if dev and int(dev["defaultSampleRate"]) != RAOP_RATE:
            self.tip.pack(fill="x", pady=(8, 0))
        else:
            self.tip.pack_forget()

    def _resampler_key(self) -> str:
        label = self.resampler_seg.get()
        return next(k for k, v in self._resampler_labels.items() if v == label)

    def _on_resampler(self, label: str) -> None:
        update_config(resampler=self._resampler_key())
        self._update_source_info()
        self._settings_changed()

    def _on_theme(self, label: str) -> None:
        key = next(k for k, v in self._theme_labels.items() if v == label)
        update_config(theme=key)
        ctk.set_appearance_mode(key)

    def _on_debug(self) -> None:
        on = bool(self.debug_switch.get())
        update_config(debug=on)
        _configure_log_levels(on)

    def _settings_changed(self) -> None:
        if self.controller.state == State.STREAMING:
            self.settings_note.configure(text="Changes apply when the stream is restarted.")

    def _settings(self) -> LiveSettings:
        cfg = load_config()
        return LiveSettings(
            loopback=cfg["loopback"],
            prebuffer_ms=int(cfg["prebuffer_ms"]),
            max_buffer_ms=int(cfg["max_buffer_ms"]),
            resampler=self._resampler_key(),
            stats_interval=2.0,
        )

    def _on_main_button(self) -> None:
        state = self.controller.state
        if state in (State.STREAMING, State.CONNECTING, State.RECONNECTING):
            self.controller.stop()
            return
        dev = self._selected_device()
        if dev:
            self.settings_note.configure(text="")
            self.controller.start(dev, self._settings(), float(self.vol_slider.get()))

    def _on_volume(self, value: float) -> None:
        self._vol_touched = time.monotonic()
        self._update_volume_label(value)
        if self._vol_pending is None:
            self.after(120, self._flush_volume)
        self._vol_pending = value

    def _flush_volume(self) -> None:
        value, self._vol_pending = self._vol_pending, None
        if value is None:
            return
        self.controller.set_volume(value)
        update_config(volume=round(value))

    def _update_volume_label(self, value: float) -> None:
        self.vol_value.configure(text=f"{round(value)}")
        glyph = "mute" if value < 1 else "vol1" if value < 34 else "vol2" if value < 67 else "vol3"
        self.vol_icon.configure(text=GLYPH[glyph])

    def _on_close(self) -> None:
        self.withdraw()
        self.controller.shutdown()
        self.destroy()

    # ---------------------------------------------------------- state/events
    def _update_main_button(self) -> None:
        state = self.controller.state
        if state == State.STREAMING:
            self.main_button.configure(text="■   Stop", state="normal", fg_color=C["danger"],
                                       hover_color=C["danger_hover"])
        elif state in (State.CONNECTING, State.RECONNECTING):
            label = "Connecting…" if state == State.CONNECTING else "Reconnecting…"
            self.main_button.configure(text=f"{label}   (cancel)", state="normal", fg_color=C["warn"],
                                       hover_color=C["warn"])
        elif state == State.STOPPING:
            self.main_button.configure(text="Stopping…", state="disabled")
        else:
            ready = self._selected_device() is not None
            self.main_button.configure(text="▶   Start Streaming", state="normal" if ready else "disabled",
                                       fg_color=C["accent"], hover_color=C["accent_hover"])

    def _apply_state(self, state: State, message: str) -> None:
        label, color = STATE_STYLE[state]
        self.pill_text.configure(text=label)
        self.pill_dot.configure(text_color=C[color])
        busy = state in ACTIVE_STATES
        for w in (self.device_menu, self.source_menu, self.device_refresh, self.source_refresh):
            w.configure(state="disabled" if busy else "normal")
        self._base_message = message
        color = C["danger"] if state == State.ERROR else C["warn"] if state == State.RECONNECTING else C["muted"]
        self.message.configure(text=message, text_color=color)
        if state in (State.IDLE, State.ERROR):
            self.settings_note.configure(text="")
            self._update_source_info()
        self._update_main_button()

    def _handle_event(self, ev: Event) -> None:
        if ev.kind == "state":
            self._apply_state(ev.data, ev.message)
        elif ev.kind == "devices":
            self._on_devices(ev.data, ev.message)
        elif ev.kind == "loopbacks":
            self._on_loopbacks(ev.data, ev.message)
        elif ev.kind in ("started", "source"):
            self._update_source_info(ev.data)
        elif ev.kind == "default_output":
            if self.controller.state not in ACTIVE_STATES:
                self.refresh_loopbacks()  # keep "System default" info current while idle
        elif ev.kind == "stats":
            self._on_stats(ev.data)
        elif ev.kind == "firewall":
            self._on_firewall(ev.data, ev.message)
        elif ev.kind == "volume":
            if time.monotonic() - self._vol_touched > 1.5:  # sync only if the user isn't dragging
                self.vol_slider.set(ev.data)
                self._update_volume_label(ev.data)

    def _on_devices(self, devices: list[AirPlayDevice], message: str) -> None:
        self.devices = {}
        for dev in devices:
            key = dev.name if dev.name not in self.devices else f"{dev.name} ({dev.address})"
            self.devices[key] = dev
        self.device_refresh.configure(state="normal")
        if not self.devices:
            self.device_menu.configure(values=["No speakers found"], state="disabled")
            self.device_menu.set("No speakers found")
            self.device_info.configure(text=message or "Make sure you're on the same network, then refresh.")
            self._update_main_button()
            return
        names = list(self.devices)
        self.device_menu.configure(values=names, state="normal")
        last = self.cfg.get("last_device")
        chosen = next((n for n, d in self.devices.items() if d.identifier == last), names[0])
        self.device_menu.set(chosen)
        self._on_device_selected(chosen)

    def _on_loopbacks(self, devs: list[dict], message: str) -> None:
        self.loopbacks = {display_name(d): d for d in devs}
        self._default_loopback = next((d for d in devs if d.get("is_default")), None)
        values = [DEFAULT_SOURCE, *self.loopbacks]
        self.source_menu.configure(values=values)
        saved = load_config()["loopback"]
        chosen = next((n for n, d in self.loopbacks.items() if d["name"] == saved), DEFAULT_SOURCE)
        self.source_menu.set(chosen)
        self._update_source_info()
        if message:
            self.source_info.configure(text=message)

    def _on_stats(self, s: dict) -> None:
        if self.controller.state != State.STREAMING:
            return
        if s["underruns"]:
            text = f"{s['underruns']} dropouts · increase the jitter buffer in Settings"
            color = C["warn"]
        else:
            text = f"buffer {s['queued_ms']:.0f} ms · latency ≈ 2 s"
            color = C["muted"]
        self.message.configure(text=f"{self._base_message}\n{text}", text_color=color)

    def _tick(self) -> None:
        """Every 50 ms: process events, logs and the level meter."""
        try:
            while True:
                self._handle_event(self.controller.events.get_nowait())
        except queue.Empty:
            pass

        lines = []
        try:
            while len(lines) < 200:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_box.insert("end", "\n".join(lines) + "\n")
            if int(self.log_box.index("end-1c").split(".")[0]) > 1000:
                self.log_box.delete("1.0", "200.0")
            self.log_box.see("end")

        # dB-scaled level meter (-60..0 dBFS) with smooth decay
        peak = self.controller.level
        target = max(0.0, 1 + 20 * math.log10(peak) / 60) if peak > 0 else 0.0
        self._meter = target if target > self._meter else self._meter * 0.88
        self.meter.set(self._meter)
        color = C["track"] if self._meter < 0.01 else C["danger"] if peak >= 0.99 else C["ok"]
        if color != self._meter_color:  # avoid needless redraws
            self._meter_color = color
            self.meter.configure(progress_color=color)

        self.after(50, self._tick)


def _configure_log_levels(debug: bool) -> None:
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("pyatv").setLevel(logging.DEBUG if debug else logging.WARNING)


def run_gui(debug: bool = False) -> int:
    """Start the interface; returns when the window closes."""
    cfg = load_config()
    log_queue: queue.Queue[str] = queue.Queue()
    root = logging.getLogger()
    root.addHandler(QueueLogHandler(log_queue))
    if sys.stderr:  # there is no stderr under pythonw
        logging.basicConfig(format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    _configure_log_levels(debug or cfg["debug"])

    if sys.platform == "win32":
        # Single window: if the app is already open, bring it to the front
        if acquire_named_mutex("Local\\HomePodStreamer.GUI") is None:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, APP_NAME)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            return 0
        # Show our own icon in the taskbar instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HomePodStreamer")
    ctk.set_appearance_mode(cfg["theme"])
    ctk.set_default_color_theme("blue")

    controller = Controller()
    app = App(controller, log_queue)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_gui("--debug" in sys.argv))
