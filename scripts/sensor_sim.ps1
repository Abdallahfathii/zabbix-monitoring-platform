# Simple MQTT sensor publisher (raw TCP, no libraries)
param(
    [string]$Broker = "172.20.10.2",
    [int]$Port = 1883,
    [string]$Topic = "sensors/sensor-001",
    [string]$Payload = '{"temperature": 25.5, "humidity": 55, "pressure": 1013, "battery": 88}'
)

function Encode-String($s) {
    $b = [System.Text.Encoding]::UTF8.GetBytes($s)
    return [byte[]]@([byte](($b.Length -shr 8) -band 0xFF), [byte]($b.Length -band 0xFF)) + $b
}

$tcp = New-Object System.Net.Sockets.TcpClient
$tcp.Connect($Broker, $Port)
$stream = $tcp.GetStream()

# MQTT CONNECT packet
$varHeader = [byte[]](0x00, 0x04) + [System.Text.Encoding]::UTF8.GetBytes("MQTT") + [byte[]](0x04, 0x02)
$keepAlive = [byte[]](0x00, 0x3C)
$clientId = Encode-String "powershell-sensor"
$payloadBytes = $varHeader + $keepAlive + $clientId
$connect = [byte[]](0x10, $payloadBytes.Length) + $payloadBytes
$stream.Write($connect, 0, $connect.Length)

# Read CONNACK
$buf = New-Object byte[] 4
$stream.Read($buf, 0, 4) | Out-Null
if ($buf[3] -ne 0) { Write-Output "CONNACK error: $($buf[3])"; exit 1 }
Write-Output "Connected to ${Broker}:${Port}"

# MQTT PUBLISH packet (QoS 0)
$topicBytes = Encode-String $Topic
$payloadUtf8 = [System.Text.Encoding]::UTF8.GetBytes($Payload)
$remaining = $topicBytes.Length + $payloadUtf8.Length
$publish = [byte[]](0x30, $remaining) + $topicBytes + $payloadUtf8
$stream.Write($publish, 0, $publish.Length)
Write-Output "Published to ${Topic}: ${Payload}"

Start-Sleep -Seconds 1
$tcp.Close()
Write-Output "Disconnected."
