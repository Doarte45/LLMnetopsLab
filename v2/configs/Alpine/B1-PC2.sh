cat > /etc/network/interfaces <<'CFG'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address 10.1.1.11/24
    gateway 10.1.1.1
CFG
rc-service networking restart
setup-hostname B1-PC2
rc-service hostname restart
