param(
    [string]$MusicRoot = ".\media\Music",
    [string]$PlaylistPath = ".\media\Playlists\all-shuffled.m3u"
)

if (-not (Test-Path $MusicRoot)) {
    Write-Error "Music root not found: $MusicRoot"
    exit 1
}

$repoRoot = (Resolve-Path ".").Path
$allSongs = Get-ChildItem -Path $MusicRoot -Recurse -File |
    Where-Object { $_.Extension.ToLower() -in @(".mp3", ".wav", ".flac", ".ogg", ".m4a") }

if (-not $allSongs) {
    Write-Error "No audio files found under $MusicRoot"
    exit 1
}

$shuffled = $allSongs | Sort-Object { Get-Random }

$playlistDir = Split-Path $PlaylistPath -Parent
if (-not (Test-Path $playlistDir)) {
    New-Item -ItemType Directory -Path $playlistDir -Force | Out-Null
}

$lines = foreach ($song in $shuffled) {
    $relative = $song.FullName.Substring($repoRoot.Length).TrimStart('\')
    "/home/pipboy2/piboy/" + ($relative -replace '\\', '/')
}

Set-Content -Path $PlaylistPath -Value $lines -Encoding UTF8

Write-Host "Created shuffled playlist:"
Write-Host $PlaylistPath
Write-Host "Track count: $($lines.Count)"