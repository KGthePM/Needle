param([string]$OutputPath = (Join-Path $PSScriptRoot 'needle.ico'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256)
$images = @()

try {
    foreach ($size in $sizes) {
        $scale = [double]$size / 32
        $bitmap = [Drawing.Bitmap]::new($size, $size, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        $background = $null
        $arc = $null
        $needle = $null
        $hub = $null
        $stream = $null
        try {
            $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
            $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.Clear([Drawing.Color]::Transparent)

            $background = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 42, 45, 50))
            $graphics.FillEllipse($background, 1 * $scale, 1 * $scale, 30 * $scale, 30 * $scale)

            $arc = [Drawing.Pen]::new([Drawing.Color]::ForestGreen, 4 * $scale)
            $arc.StartCap = [Drawing.Drawing2D.LineCap]::Round
            $arc.EndCap = [Drawing.Drawing2D.LineCap]::Round
            $graphics.DrawArc($arc, 5 * $scale, 5 * $scale, 22 * $scale, 22 * $scale, 135, 270)

            $angle = (135 + 270 * 0.7) * [Math]::PI / 180
            $needle = [Drawing.Pen]::new([Drawing.Color]::White, 2 * $scale)
            $graphics.DrawLine(
                $needle,
                16 * $scale,
                16 * $scale,
                (16 + 9 * [Math]::Cos($angle)) * $scale,
                (16 + 9 * [Math]::Sin($angle)) * $scale
            )
            $hub = [Drawing.SolidBrush]::new([Drawing.Color]::White)
            $graphics.FillEllipse($hub, 13 * $scale, 13 * $scale, 6 * $scale, 6 * $scale)

            $stream = [IO.MemoryStream]::new()
            $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
            $images += ,$stream.ToArray()
        }
        finally {
            if ($stream) { $stream.Dispose() }
            if ($hub) { $hub.Dispose() }
            if ($needle) { $needle.Dispose() }
            if ($arc) { $arc.Dispose() }
            if ($background) { $background.Dispose() }
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }

    $output = [IO.MemoryStream]::new()
    $writer = [IO.BinaryWriter]::new($output)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$images.Count)
        $offset = 6 + 16 * $images.Count
        for ($index = 0; $index -lt $images.Count; $index++) {
            $size = $sizes[$index]
            $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
            $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$images[$index].Length)
            $writer.Write([uint32]$offset)
            $offset += $images[$index].Length
        }
        foreach ($image in $images) { $writer.Write($image) }
        $writer.Flush()
        [IO.File]::WriteAllBytes([IO.Path]::GetFullPath($OutputPath), $output.ToArray())
    }
    finally {
        $writer.Dispose()
        $output.Dispose()
    }
}
finally {
    $images = @()
}
