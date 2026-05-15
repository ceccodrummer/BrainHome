# Set firewall rules for Brain-Home services on the Tailscale interface.
# Run this script in an elevated PowerShell session.

$tailscaleLocalIp = '100.87.153.12'
$tailscaleNet = '100.64.0.0/10'

New-NetFirewallRule -DisplayName 'BrainHome Allow Tailscale FastAPI' \
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 \
  -LocalAddress $tailscaleLocalIp -RemoteAddress $tailscaleNet -Profile Any

New-NetFirewallRule -DisplayName 'BrainHome Allow Tailscale Dify' \
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3001 \
  -LocalAddress $tailscaleLocalIp -RemoteAddress $tailscaleNet -Profile Any

Write-Host 'Firewall rules created successfully.'
