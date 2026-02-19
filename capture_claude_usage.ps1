# Claude Usage Monitor - PowerShell Script
# Captures screenshots of Claude usage page every minute

param(
    [int]$IntervalSeconds = 60,
    [string]$OutputDir = $PSScriptRoot
)

# Fix truncated captures on HiDPI displays by making this process DPI-aware.
# Without this, Windows can DPI-virtualize screen coordinates for non-DPI-aware
# processes, causing CopyFromScreen to capture only a portion of the desktop.
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class DpiAwareness
{
    [DllImport("user32.dll")]
    public static extern IntPtr SetProcessDpiAwarenessContext(IntPtr dpiAwarenessContext);

    [DllImport("user32.dll")]
    public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiAwarenessContext);

    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
}

public static class DisplaySettings
{
    private const int ENUM_CURRENT_SETTINGS = -1;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DEVMODE
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string dmDeviceName;
        public short dmSpecVersion;
        public short dmDriverVersion;
        public short dmSize;
        public short dmDriverExtra;
        public int dmFields;

        public int dmPositionX;
        public int dmPositionY;
        public int dmDisplayOrientation;
        public int dmDisplayFixedOutput;

        public short dmColor;
        public short dmDuplex;
        public short dmYResolution;
        public short dmTTOption;
        public short dmCollate;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel;
        public int dmPelsWidth;
        public int dmPelsHeight;
        public int dmDisplayFlags;
        public int dmDisplayFrequency;
        public int dmICMMethod;
        public int dmICMIntent;
        public int dmMediaType;
        public int dmDitherType;
        public int dmReserved1;
        public int dmReserved2;
        public int dmPanningWidth;
        public int dmPanningHeight;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

    public static bool TryGetCurrentMode(string deviceName, out int x, out int y, out int width, out int height)
    {
        var dm = new DEVMODE();
        dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        if (!EnumDisplaySettings(deviceName, ENUM_CURRENT_SETTINGS, ref dm))
        {
            x = y = width = height = 0;
            return false;
        }

        x = dm.dmPositionX;
        y = dm.dmPositionY;
        width = dm.dmPelsWidth;
        height = dm.dmPelsHeight;
        return width > 0 && height > 0;
    }
}
"@ -ErrorAction Stop

    # Prefer Per-Monitor v2 when available (Windows 10+); fall back to system DPI aware.
    $result = [DpiAwareness]::SetProcessDpiAwarenessContext([IntPtr](-4))
    if ($result -eq [IntPtr]::Zero) {
        [DpiAwareness]::SetProcessDPIAware() | Out-Null
    }
}
catch {
    # Best effort: if this fails, proceed with the legacy behavior.
}

# Create output directory if it doesn't exist
if (!(Test-Path -Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
    Write-Host "Created directory: $OutputDir"
}

Add-Type -AssemblyName System.Windows.Forms

# When running under PowerShell 7+ (.NET), this opts Windows Forms into HiDPI.
# On Windows PowerShell 5.1 (.NET Framework) this API doesn't exist.
try {
    [System.Windows.Forms.Application]::SetHighDpiMode([System.Windows.Forms.HighDpiMode]::PerMonitorV2)
}
catch {
}

Add-Type -AssemblyName System.Drawing

function Capture-Screenshot {
    param([string]$FilePath)

    # Force per-monitor DPI for this thread while reading bounds + capturing.
    $prevDpiContext = [IntPtr]::Zero
    try {
        $prevDpiContext = [DpiAwareness]::SetThreadDpiAwarenessContext([IntPtr](-4))
    }
    catch {
        $prevDpiContext = [IntPtr]::Zero
    }

    try {
        # Get the screen bounds
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen
        $bounds = $screen.Bounds

        # On some HiDPI setups, WinForms bounds are DPI-scaled even when capture uses physical pixels.
        # Ask Windows for the current display mode to get the real pixel dimensions.
        $x = 0
        $y = 0
        $w = 0
        $h = 0
        try {
            if ([DisplaySettings]::TryGetCurrentMode($screen.DeviceName, [ref]$x, [ref]$y, [ref]$w, [ref]$h)) {
                $bounds = New-Object System.Drawing.Rectangle $x, $y, $w, $h
            }
        }
        catch {
        }

        # Create bitmap
        $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)

        # Capture screen
        $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)

        # Save to file
        $bitmap.Save($FilePath, [System.Drawing.Imaging.ImageFormat]::Png)

        # Cleanup
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    finally {
        if ($prevDpiContext -ne [IntPtr]::Zero) {
            try { [DpiAwareness]::SetThreadDpiAwarenessContext($prevDpiContext) | Out-Null } catch {}
        }
    }
}

Write-Host "Claude Usage Monitor Started"
Write-Host "Screenshots will be saved to: $OutputDir"
Write-Host "Interval: $IntervalSeconds seconds"
Write-Host "Press Ctrl+C to stop"
Write-Host ""

$count = 0
while ($true) {
    $count++
    $timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
    $filename = Join-Path $OutputDir "claude_usage_$timestamp.png"

    try {
        Capture-Screenshot -FilePath $filename
        Write-Host "[$count] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - Screenshot saved: $filename"
    }
    catch {
        Write-Host "[$count] Error capturing screenshot: $_" -ForegroundColor Red
    }

    Start-Sleep -Seconds $IntervalSeconds
}
