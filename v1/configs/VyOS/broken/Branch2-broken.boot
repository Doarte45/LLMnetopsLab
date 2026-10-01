interfaces {
    ethernet eth0 {
        address 10.10.6.1/23
    }
    ethernet eth1 {
        address 172.16.7.10/30
    }
    ethernet eth2 {
        address 172.16.7.6/30
        disable
    }
}
protocols {
    ospf {
        area 0 {
            network 10.10.4.0/22
            network 172.16.7.4/30
            network 172.16.7.8/30
        }
        passive-interface eth2
    }
}
system {
    host-name Branch2
}
