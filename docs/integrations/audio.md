# Host audio

Off by default and global only. When enabled, workspace boxes get the host's
sound server, so an agent CLI inside the box can use the microphone and
speakers — today, Claude Code's voice dictation (`/voice`). Inside the box the
ALSA default device goes through the image's PulseAudio plugin to the host's
socket at `/run/user/<uid>/pulse/native`, served by PipeWire or PulseAudio.

## Enabling it

```bash
agent-box config    # Workspace box → Audio: Space toggles it on
agent-box build     # once, if the image predates the audio client layer
agent-box claude
```

In Claude Code run `/voice`. Dictation also needs a Claude.ai login; it is not
available with an API key.

The host needs a PulseAudio-compatible server: PulseAudio itself or PipeWire's
`pipewire-pulse`, which desktop distributions run by default. If
`/run/user/<uid>/pulse/native` is not a socket when a box is created, the box
starts without sound, and every launch prints a warning while that lasts.

The socket is part of the box's identity. After turning the switch on or off,
a stopped workspace box is recreated on the next launch; a running one refuses
the launch as incompatible until you stop it with `agent-box stop`. With the
switch on, the same happens when the socket appears or disappears while a box
is running, for example when the sound server is started or stopped.

## What it exposes

Everything in the box shares one trust boundary, so the agent, the tools it
runs and the project's own code can all:

- record from any host input device;
- play through the host's speakers;
- control the sound server, including volumes and default devices.

The PulseAudio protocol does not separate these, so there is no
microphone-only mode. `/dev/snd` is never passed through. Leave the switch off
unless you use voice in the box.
