TrimUI Brick emulator - Windows on ARM64
========================================

Requirements
  * Windows 11 24H2 build 26100.3915 or newer on an ARM64 PC.
  * "Windows Hypervisor Platform" enabled:
      Win+R -> optionalfeatures -> tick "Windows Hypervisor Platform" -> OK -> reboot.

Run
  Double-click Brick.cmd. On the first start your SD card (data\sdcard.qcow2)
  is created from the bundled custom firmware, which then installs itself
  exactly as on a real Brick. Click into the window to give it the keyboard.

Controls
  D-pad = arrows   A/B/X/Y = X/Z/S/A   L/R/L2/R2 = Q/W/E/R
  Start = Enter    Select = Right Shift   Menu = Esc   F1/F2 = 1/2
  Volume = = / -   Power = P

Options (from a terminal in this folder)
  Brick.cmd -SD path\to\card.qcow2   use another SD card image (.qcow2 or raw .img)
  Brick.cmd -Reset                   factory-reset internal storage
  Brick.cmd -Accel tcg               run without the hypervisor (slow)
  Brick.cmd -Gpu virgl               experimental GPU-accelerated graphics
  Brick.cmd -Display gtk             GTK window instead of SDL

Your data lives in the data\ folder; delete it to start over.
