
# TODO: Add this step to the dev README
# Must install the vagrant hostmanager plugin
# vagrant plugin install vagrant-hostmanager

Vagrant.configure("2") do |config|

  config.vm.box = "bento/ubuntu-24.04"
  config.hostmanager.enabled = true
  config.hostmanager.manage_host = true

    config.vm.provision "shell", name: "global", keep_color: true, inline: <<-SHELL

        # Update package list
        apt update

        # Install helpful packages
        apt install -y  make      \
                        net-tools \
                        tree      \

        # Ensure python points to python3
        ln -s /usr/bin/python3 /usr/bin/python

        # Set CLI mode to vi
        echo 'set -o vi' >> /home/vagrant/.bashrc

        # Set timezone to Chicago
        sudo timedatectl set-timezone America/Chicago

        # Clean up
        apt clean
    SHELL

    config.vm.provision "shell", name: "global", privileged: false, keep_color: true, inline: <<-SHELL

        # Set CLI mode to vi for the default non-root user
        echo 'set -o vi' >> ~/.bashrc
    SHELL

    # Publicly accessible VM
    config.vm.define "dev", primary: true do |dev|

        dev.vm.provider "virtualbox" do |vb|
            vb.cpus = 4
        end

        # Set the hostname for the host machine
        dev.vm.hostname = "otto.dev"

        # Disable the default SSH forwarding rule and enable a unique one
        dev.vm.network "forwarded_port", guest: 22, host: 5678

        # Private network (shared with test VMs)
        dev.vm.network "private_network", ip: "10.10.200.100"

        dev.vm.provision "shell", name: "dev-root", keep_color: true, inline: <<-SHELL

            # Install GitHub client and development tools for coverage testing
            apt install -y  gcc     \
                            gh      \
                            lcov    \

            # Set MTU to 1350 on all ethernet interfaces to support mobile
            # connections that have a smaller MTU size
            printf '[Match]\\nType=ether\\n\\n[Link]\\nMTUBytes=1350\\n' > /etc/systemd/network/10-mtu.link
        SHELL

        dev.vm.provision "shell", name: "dev", privileged: false, keep_color: true, inline: <<-SHELL

            # Set the default branch name to `main` instead of `master`
            git config --global init.defaultBranch main

            # Install uv via the official install script
            curl -LsSf https://astral.sh/uv/install.sh | sh

            # Set up shell completions for uv and uvx
            echo 'eval "$(uv generate-shell-completion bash)"' >> ~/.bashrc
            echo 'eval "$(uvx --generate-shell-completion bash)"' >> ~/.bashrc

            echo 'git config --global core.editor "vim"' >> ~/.bashrc
            echo 'alias gs="git status"' >> ~/.bashrc
        SHELL
    end

    config.vm.define "test1", autostart: false do |test1|
        test1.vm.network "private_network", ip: "10.10.200.11"

        # Apply test provisioning
        provision_test_vm(test1, "test1")
    end

    config.vm.define "test2", autostart: false do |test2|
        test2.vm.network "private_network", ip: "10.10.200.12"

        # Apply test provisioning
        provision_test_vm(test2, "test2")
    end

    config.vm.define "test3", autostart: false do |test3|
        test3.vm.network "private_network", ip: "10.10.200.13"

        # Apply test provisioning
        provision_test_vm(test3, "test3")
    end

    # Dynamically set hostname based on VM name
    def set_hostname(vm, name, domain = nil)
        base = name.gsub("_", "-")
        vm.vm.hostname = domain ? "#{base}.#{domain}" : base
    end

    # Run common test VM provisioning steps
    def provision_test_vm(vm, name)
        set_hostname(vm, name)
        vm.vm.provision "shell", name: "common test", keep_color: true, inline: <<-SHELL

            # install SSH and Telnet server
            apt -y install  net-tools \
                            telnetd   \
                            vsftpd    \

            # Uncomment telnet from the inetd config file, then start and enable inetd
            sed -i 's/#<off># *//' /etc/inetd.conf
            systemctl enable --now inetutils-inetd.service

            # Enable FTP write access (needed for uploads) and disable the seccomp
            # sandbox (vsftpd 3.0.5+ on Ubuntu 24.04 blocks writes by default),
            # then start and enable vsftpd
            sed -i 's/#write_enable=YES/write_enable=YES/' /etc/vsftpd.conf
            grep -q seccomp_sandbox /etc/vsftpd.conf || echo 'seccomp_sandbox=NO' >> /etc/vsftpd.conf
            systemctl enable --now vsftpd

            # Set the vagrant user's password to 'vagrant' for SSH/telnet access
            echo 'vagrant:vagrant' | sudo chpasswd

            # Create an additional user 'test' for SSH and telnet access.
            useradd -m -s /bin/bash test
            echo 'test:Password1' | chpasswd
        SHELL
    end

    # Build and install guest additions if the kernel version changes
    # NOTE: This function is currently not used, but could be useful in the future
    # if a box's kernel updates and causes the guest additions kernel modules to
    # no longer insert into the guest OS.
    def apply_guest_additions(vm)
        vm.vm.provision "shell", name: "guest additions", keep_color: true, inline: <<-SHELL

            # Ensure packages for vbguest plugins are available
            apt -y install  build-essential           \
                            dkms                      \
                            gcc                       \
                            linux-headers-$(uname -r) \
                            make                      \
                            perl                      \

            # NOTE: The version used should match the version of your VirtualBox software
            # Possibly parameterize the provision script by grabbing the first argument and defaulting to 7.2.6
            # See https://developer.hashicorp.com/vagrant/docs/provisioning/shell#args for how to parameterize
            vbox_ver="${1:-7.2.6}"

            wget http://download.virtualbox.org/virtualbox/${vbox_ver}/VBoxGuestAdditions_${vbox_ver}.iso -P /tmp
            mount -o loop /tmp/VBoxGuestAdditions_${vbox_ver}.iso /mnt

            # Run the appropriate Linux Additions script based on the ISA
            case "$(uname -m)" in
            x86_64|i?86)
                sh -x /mnt/VBoxLinuxAdditions.run
                ;;
            aarch64|arm64)
                sh -x /mnt/VBoxLinuxAdditions-arm64.run
                ;;
            *)
                echo "Unknown or unsupported architecture: $(uname -m)"
                ;;
            esac

            /opt/VBoxGuestAdditions*/init/vboxadd setup
        SHELL
    end

end
