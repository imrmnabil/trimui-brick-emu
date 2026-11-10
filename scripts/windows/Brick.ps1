<#
  TrimUI Brick emulator for Windows (ARM64).

  Runs the emulated Brick in QEMU with the Windows Hypervisor Platform (WHPX),
  so the Brick's ARM64 code runs directly on your CPU.

    Brick.cmd                       boot data\sdcard.qcow2 (created from the bundled card on first run)
    Brick.cmd -SD D:\cards\x.qcow2  boot another SD card image (raw .img works too)
    Brick.cmd -Accel tcg            no hypervisor (slow software CPU) - for testing
    Brick.cmd -Gpu virgl            experimental host-GPU GLES (ANGLE/Direct3D)
    Brick.cmd -Reset                factory-reset the internal storage (eMMC)

  Requires Windows 11 24H2 (26100.3915+) with "Windows Hypervisor Platform"
  enabled (optionalfeatures.exe).
#>
param(
    [string]$SD = "",
    [string]$Emmc = "",
    [ValidateSet("whpx", "tcg")] [string]$Accel = "whpx",
    [ValidateSet("soft", "virgl")] [string]$Gpu = "soft",
    [ValidateSet("sdl", "gtk")] [string]$Display = "sdl",
    [switch]$Reset
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Img = Join-Path $Root "images"
$Data = Join-Path $Root "data"
$Qemu = Join-Path $Root "qemu\qemu-system-aarch64.exe"
New-Item -ItemType Directory -Force -Path $Data | Out-Null

# Per-user writable copies of the factory images.
if (-not $Emmc) { $Emmc = Join-Path $Data "emmc.qcow2" }
if ($Reset -or -not (Test-Path $Emmc)) {
    Copy-Item (Join-Path $Img "emmc-factory.qcow2") $Emmc -Force
    Write-Host "internal storage (eMMC) initialised: $Emmc"
}
if (-not $SD) {
    $SD = Join-Path $Data "sdcard.qcow2"
    $bundled = Get-ChildItem $Img -Filter "sd-*.qcow2" | Select-Object -First 1
    if (-not (Test-Path $SD) -and $bundled) {
        Copy-Item $bundled.FullName $SD
        Write-Host "SD card created from $($bundled.Name): $SD"
    }
}
function Fmt([string]$p) { if ($p -match '\.qcow2$') { "qcow2" } else { "raw" } }

$parts = "bootloader@mmcblk0p1:env@mmcblk0p2:env-redund@mmcblk0p3:boot@mmcblk0p4:rootfs@mmcblk0p5:" +
         "rootfs_data@mmcblk0p6:private@mmcblk0p7:recovery@mmcblk0p8:pstore@mmcblk0p9:UDISK@mmcblk0p10"
$cmdline = "console=ttyAMA0 root=/dev/mmcblk0p5 rootwait init=/sbin/init rdinit=/rdinit loglevel=1 " +
           "partitions=$parts cma=64M gpt=1 snd_hda_intel.id=audiocodec " +
           "gpio_mockup.gpio_mockup_ranges=0,384 video=Virtual-1:1024x768 brick.gpu=$Gpu"

if ($Accel -eq "whpx") { $cpu = "host"; $acc = "whpx" } else { $cpu = "cortex-a53"; $acc = "tcg,thread=multi" }
$gpuDev = if ($Gpu -eq "virgl") { "virtio-gpu-gl-pci,xres=1024,yres=768" } else { "virtio-gpu-pci,xres=1024,yres=768" }
$disp = if ($Gpu -eq "virgl") { "$Display,gl=on" } else { $Display }

$qargs = @(
    "-name", "TrimUI Brick",
    "-M", "virt", "-cpu", $cpu, "-smp", "4", "-m", "1024", "-accel", $acc,
    "-kernel", (Join-Path $Img "Image"), "-append", $cmdline,
    # eMMC = mmcblk0, SD slot = mmcblk1 (real SD/MMC protocol over SDHCI)
    "-device", "sdhci-pci,id=emmc-host,addr=0x5",
    "-drive", "if=none,id=emmc,file=$Emmc,format=$(Fmt $Emmc)",
    "-device", "sd-card,drive=emmc,bus=/gpex-pcihost/pcie.0/emmc-host/sd-bus",
    "-device", "sdhci-pci,id=sd-host,addr=0x6",
    "-drive", "if=none,id=emu,file=$(Join-Path $Img 'brick-emu.qcow2'),format=qcow2,readonly=on",
    "-device", "virtio-blk-pci,drive=emu,addr=0x7",
    "-device", "virtio-keyboard-pci",
    "-device", "qemu-xhci", "-device", "usb-tablet",
    "-device", $gpuDev,
    "-display", $disp,
    "-audiodev", "dsound,id=snd0", "-device", "intel-hda", "-device", "hda-duplex,audiodev=snd0",
    "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
    "-device", "virtio-serial-pci,id=vser", "-chardev", "null,id=con0", "-chardev", "null,id=shell0",
    "-device", "virtconsole,chardev=con0,bus=vser.0,nr=0", "-device", "virtconsole,chardev=shell0,bus=vser.0,nr=1",
    "-serial", "mon:stdio"
)
if ($SD -and (Test-Path $SD)) {
    $qargs += @("-drive", "if=none,id=sd,file=$SD,format=$(Fmt $SD)",
                "-device", "sd-card,drive=sd,bus=/gpex-pcihost/pcie.0/sd-host/sd-bus")
} else {
    Write-Host "note: no SD card image - booting stock firmware with an empty slot"
}

Write-Host "Starting TrimUI Brick ($acc). Keys: arrows=D-pad X/Z/S/A=A/B/X/Y Q/W/E/R=L/R/L2/R2 Enter=Start RShift=Select Esc=Menu P=Power"
Write-Host "This window is the Brick's serial console (Ctrl-a x quits)."
& $Qemu @qargs
if ($LASTEXITCODE -ne 0 -and $Accel -eq "whpx") {
    Write-Host ""
    Write-Host "QEMU failed with WHPX. Make sure 'Windows Hypervisor Platform' is enabled"
    Write-Host "(optionalfeatures.exe, then reboot), or try:  Brick.cmd -Accel tcg"
}
