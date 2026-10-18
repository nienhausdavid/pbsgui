# Auf der ersten Konsole startet automatisch der Wiederherstellungs-Assistent.
if [ "$(tty)" = /dev/tty1 ] && [ -z "$PBSGUI_NO_AUTOSTART" ]; then
    pbsgui rescue
    echo
    echo "Wiederherstellungs-Assistent beendet. Erneut starten: pbsgui rescue    Netzwerk: nmtui    Neustart: reboot"
fi
