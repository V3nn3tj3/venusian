#!/bin/bash
#
# Author: Wiebe Cazemier <wiebe@ytec.nl>
#
# Script to establish a remote SSH tunnel to a designated host, so that Victron
# has direct access to each color control that has remote support enabled.
#
# Keep this script running with daemon tools. If it exits because the
# connection crashes, or whatever, daemon tools will start a new one.
#

here=$(dirname "$0")

terminated()
{
  [ -n "$ssh_pid" ] && echo "Terminate ssh tunnel with PID $ssh_pid"
  [ -n "$ssh_pid" ] && kill -s 0 $ssh_pid 2> /dev/null && kill $ssh_pid && ssh_pid=""
  [ -f "$ssh_output_file" ] && rm -- "$ssh_output_file"
  # Write 0 in RemoteSupportIpAndPort to remove invalid portnumber
  dbus-send --system --type=method_call --dest=com.victronenergy.settings /Settings/System/RemoteSupportIpAndPort com.victronenergy.BusItem.SetValue string:0
  sleep 2
  exit 0
}

trap "terminated" SIGINT SIGTERM

if [ -z "$target_host" ]; then
  echo "No target_host environment variable defined."
  exit 1
fi

# Wait until it is possible to add this setting and dbus and localsetting is up
until dbus-send --print-reply --session --dest=com.victronenergy.settings /Settings com.victronenergy.Settings.AddSilentSetting string:"System" string:"RemoteSupportIpAndPort" string:0 string:"s" variant:int32:0 variant:int32:0 2>&1 > /dev/null; do
  sleep 1
done

ssh_output_file=/var/lib/venusian/venus/etc/ssh.tmp

setup_tunnels()
{
  target_port="$1"
  tunnel_success="false"

  # We use keep alives for the opposite reason you might think. Without them, the
  # script will think the tunnel is still active when the connection is long
  # gone. It needs to be reestablished when that happens. Daemon tools will take
  # care of that; it restarts the script when it exits.
  ssh -N -o ExitOnForwardFailure=yes \
    -o ConnectTimeout=20 \
    -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=3 \
    -o TCPKeepAlive=yes \
    -o StrictHostKeyChecking=yes \
    -o HostKeyAlias="$target_host" \
    -p "$target_port" \
    -R 0:localhost:2222 \
    -i /var/lib/venusian/venus/.ssh/id_rsa \
    "ccgxlogin@$target_host" 2> "$ssh_output_file" &

  ssh_pid=$!
  echo "Trying port $target_port. SSH tunnel process has PID $ssh_pid"

  counter=0
  echo "Trying to obtain SSH tunnel port from $ssh_output_file"
  while [ -z "$ssh_tunnel_port" -a $counter -lt 30 ]; do
    ssh_tunnel_port="$(grep -i "Allocated port.*localhost:22" "$ssh_output_file" | awk '{ print $3; };' )"
    sleep 1
    counter=$(( $counter + 1 ))
  done

  if [ -n "$ssh_tunnel_port" ]; then
    echo "SSH has PID $ssh_pid and tunnel runs on remote port $ssh_tunnel_port. Getting remote server IP address..."
    server_ip=$("$here/get_ip_of_relay_server.py" --sshpid "$ssh_pid")
    if [ "$?" -ne 0 ]; then
      echo "Something went wrong in obtaining the server IP using get_ip_of_relay_server.py"
      terminated
    fi

    echo "Remote tunnel end is $server_ip. Storing '$server_ip;$ssh_tunnel_port' in dbus."
    dbus-send --session --type=method_call --dest=com.victronenergy.settings /Settings/System/RemoteSupportIpAndPort com.victronenergy.BusItem.SetValue string:"$server_ip;$ssh_tunnel_port"

    tunnel_success="true"
  else
    echo "No SSH tunnel established. Killing SSH PID $ssh_pid should it still exist."
    kill -9 $ssh_pid
  fi

  # wait for termination. This will hang if the tunnel is up
  wait $ssh_pid 2> /dev/null
  ssh_pid=""
}

for port in 80 443 22; do
  setup_tunnels $port

  if [ "$tunnel_success" == "true" ]; then
    echo "SSH exited from a successful tunnel, so we exited 'normally' and will not try other ports"
    break
  fi
done

if [ -z "$ssh_tunnel_port" ]; then
  echo "No SSH port allocated. Terminating."
  terminated
fi

# Pitfall
terminated