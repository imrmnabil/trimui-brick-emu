# Changelog

## 0.1.1 (2026-11-11)

### Fixed
- **Screen stayed black in GPU (virgl) mode** ("Display output is not
  active"). The Brick's display output was never switched on, so NextUI and
  other CFWs ran but showed nothing. The EGL shim now renders through the DRM
  render node, and the display is switched on at boot.
- **The Brick froze for good when it went to sleep.** After about two minutes
  idle, NextUI puts the Brick into deep sleep. The emulated Brick really
  suspended and could never wake up, so the screen froze and the guest stopped
  responding. Sleep is now simulated: the Brick stays asleep until you press
  the power button, then picks up where it left off.

Existing SD card and eMMC images work as they are; nothing needs to be
recreated.

## 0.1.0 (2026-11-10)

First release.
