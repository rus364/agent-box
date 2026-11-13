# Clipboard images

Paste PNG images into interactive container Codex and Claude sessions with
**Ctrl+V**. The bridge is built in: no setting, enable step, launch flag or
container rebuild is required. New launches and `agent-box attach` use it
automatically; an already attached terminal needs reattaching to use updated
host launcher behavior.

The host needs `wl-paste` (the `wl-clipboard` package) on Wayland, or `xclip` on
X11. agent-box does not install them. Copy an image, focus the agent's prompt and
press Ctrl+V. The bridge requests only `image/png`, copies it into the box and
pastes its absolute path using terminal bracketed paste. The CLI should show an
image attachment; submit the prompt normally when ready. Other clipboard image
formats need a source application that also offers PNG.

## Terminal and remote behavior

- Interactive Codex and Claude launches, including external API sessions, work
  with tmux or `--no-tmux`. Reattaching a `codex-*` or `claude-*` session enables
  the bridge automatically. Use it while the agent's prompt has focus, rather
  than a shell, editor or tmux scroll mode.
- Raw Ctrl+V, CSI-u Ctrl+V and xterm modifyOtherKeys Ctrl+V are recognized.
  Ctrl+V inside bracketed text paste is forwarded as text, including split paste
  delimiters. Other keys pass through. A terminal binding that consumes Ctrl+V
  must be changed in that terminal before the bridge can receive it. Extremely
  delayed enhanced key sequences are forwarded rather than intercepted.
- A missing tool, unavailable desktop, non-PNG clipboard, timeout or full store
  prints a diagnostic only when Ctrl+V is pressed and forwards the original key.
  Starting or attaching a session does not probe the clipboard. The session
  continues; ordinary text paste remains the terminal's and CLI's responsibility.
- Shell sessions, batch commands, pipes and redirects do not use the bridge.
  Nested launches inside a box do not add a bridge. `--host` uses each CLI's
  native clipboard support.
- The clipboard belongs to the desktop running the host launcher. SSH sessions
  are refused even with forwarded display variables; headless hosts have no
  desktop clipboard. A browser, phone or Remote Control client cannot invoke
  this bridge. A local terminal connected to a remote-controlled session can
  still paste into its local container CLI. Files are not copied to another
  machine or to a remote backend's filesystem.

## Limits, storage and cleanup

A gesture reads at most **16 MiB**, with a **3-second** reader timeout and a
**5-second** transfer timeout. PNG dimensions are limited to 16,384 per side and
67,108,864 pixels. Only the explicit PNG selection is read; there is no text
selection query, clipboard polling, background broker or container-callable
clipboard endpoint. A Wayland failure never falls back to a different X11
selection. No host display socket or clipboard tool is mounted into the box.
The host reader and sender keep image bytes in memory and pipes; neither creates
a host temporary image file.

The box stores images in `/tmp/agent-box-clipboard-UID`, with directory mode 0700
and file mode 0600. Agents running as that container user can read the images
you transfer. A lock enforces a shared maximum of **32 files / 128 MiB** per box
user, across launches and attaches. A full store refuses another transfer; it
never evicts an earlier image that a pending prompt may still need. Failed
transfers remove their partial files where possible; an interrupted leftover
still counts against the limits.

Detaching does not remove images. With the default mounts, they disappear with
the workspace container, including when `agent-box stop` removes it. An explicit
user bind mount covering `/tmp/agent-box-clipboard-UID` (for example
`--mount /tmp`) places these files on that mount instead; they can then persist on the
host after container removal. Such mounts are separate user-granted filesystem
access and must be managed by their owner. The box's `/tmp` and any files a CLI
copies into its conversation history may be backed by disk; this is not secure
erasure. The CLI controls submission, provider retention and its own history.
`--no-tmux` retains its ordinary direct-session lifetime; this integration does
not make it detachable.

## Verification boundary

Synthetic, isolated input probes with Codex 0.168.0 and Claude Code 2.1.322
recognized a bracketed PNG path as `[Image #1]`, without submitting a prompt or
reading a real clipboard. Tests cover transfer bounds, private storage,
the absence of a configuration switch, automatic launch/attach routing and
native PTY input, resize and cleanup.
Desktop clipboard → Podman → provider delivery still needs live acceptance in
the intended environment; image support also depends on the selected model.
