param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$voice = New-Object -ComObject SAPI.SpVoice
$stream = New-Object -ComObject SAPI.SpFileStream
try {
    $stream.Open($Destination, 3, $false)
    $voice.AudioOutputStream = $stream
    $voice.Speak('Hello world. This is a timing test.') | Out-Null
} finally {
    $stream.Close()
}
