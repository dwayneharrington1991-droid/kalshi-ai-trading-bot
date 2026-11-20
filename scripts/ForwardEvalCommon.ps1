# Shared helpers for the forward-test scripts (read-only on Oracle except one temporary file in /tmp).
#
# Long single SSH transfers from this PC were cut off at about 195 KB several times on 2026-11-20,
# which would make a forward-test export fail. Get-RemoteExport therefore compresses the export into
# a temporary file on the server, downloads it in verified chunks with retries, removes the
# temporary file, and decompresses locally.

function Invoke-Ssh {
    param([string]$Key, [string]$Server, [string]$Command, [string]$StdinFile, [string]$OutFile)
    $in = if ($StdinFile) { " < `"$StdinFile`"" } else { '' }
    $out = if ($OutFile) { " > `"$OutFile`"" } else { '' }
    cmd /c "ssh -i `"$Key`" -o BatchMode=yes -o ConnectTimeout=20 $Server `"$Command`"$in$out 2>nul"
    return $LASTEXITCODE
}

function Get-RemoteExport {
    param([string]$Key, [string]$Server, [string]$RemotePython, [string]$ScriptFile, [string]$Arguments,
          [string]$Destination, [int]$ChunkBytes = 150000, [int]$Retries = 5)
    $tmp = "/tmp/kfe-" + [guid]::NewGuid().ToString('N').Substring(0, 12) + '.json.gz'
    $sizeFile = "$Destination.size"
    $rc = Invoke-Ssh -Key $Key -Server $Server -StdinFile $ScriptFile -OutFile $sizeFile `
        -Command "nice -n 19 ionice -c3 $RemotePython - $Arguments | gzip -9 > $tmp && stat -c %s $tmp"
    if ($rc -ne 0) { throw "Remote export failed (exit $rc)." }
    $size = [int64]((Get-Content $sizeFile -Raw).Trim())
    Remove-Item $sizeFile -ErrorAction SilentlyContinue
    $gz = "$Destination.gz"
    $stream = [System.IO.File]::Create($gz)
    try {
        $offset = [int64]0
        while ($offset -lt $size) {
            $want = [Math]::Min([int64]$ChunkBytes, $size - $offset)
            $ok = $false
            for ($try = 1; $try -le $Retries -and -not $ok; $try++) {
                $part = "$Destination.part"
                Invoke-Ssh -Key $Key -Server $Server -OutFile $part `
                    -Command "tail -c +$($offset + 1) $tmp | head -c $want" | Out-Null
                if ((Test-Path $part) -and (Get-Item $part).Length -eq $want) {
                    $bytes = [System.IO.File]::ReadAllBytes($part)
                    $stream.Write($bytes, 0, $bytes.Length)
                    $ok = $true
                }
                Remove-Item $part -ErrorAction SilentlyContinue
            }
            if (-not $ok) { throw "Chunk at offset $offset could not be downloaded after $Retries tries." }
            $offset += $want
        }
    } finally {
        $stream.Close()
        Invoke-Ssh -Key $Key -Server $Server -Command "rm -f $tmp" | Out-Null
    }
    $in = [System.IO.File]::OpenRead($gz)
    $zip = New-Object System.IO.Compression.GZipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
    $outFile = [System.IO.File]::Create($Destination)
    try { $zip.CopyTo($outFile) } finally { $outFile.Close(); $zip.Close(); $in.Close() }
    Remove-Item $gz -ErrorAction SilentlyContinue
    if ((Get-Item $Destination).Length -lt 1000) { throw "Export is unexpectedly small." }
}
