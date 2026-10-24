# Auf der ersten Konsole startet automatisch der Wiederherstellungs-Assistent.
if [ "$(tty)" = /dev/tty1 ] && [ -z "$PBSGUI_NO_AUTOSTART" ]; then
    # Kurz auf die Updates der Programme warten (Netzwerk und Download), eine Taste überspringt das Warten
    echo "Suche Updates für pbstui, Proxmox-Client und VPN-Werkzeuge (bis 45 s, beliebige Taste = überspringen) …"
    i=0
    while [ ! -s /run/pbsgui-update.state ] && [ "$i" -lt 45 ]; do
        read -r -t 1 -n 1 _ && break
        i=$((i + 1))
    done
    [ -s /run/pbsgui-update.state ] && echo "Update: $(cat /run/pbsgui-update.state)"
    pbsgui rescue
    echo
    echo "Wiederherstellungs-Assistent beendet. Erneut starten: pbsgui rescue    Netzwerk: nmtui    VPN: pbsgui vpn    Updates: pbsgui update    Neustart: reboot"
fi
