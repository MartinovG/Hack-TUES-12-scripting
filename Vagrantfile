Vagrant.configure("2") do |config|
  config.vm.box = ENV['HIVE_VM_BOX'] || "ubuntu/focal64"

  vm_mem = ENV['HIVE_VM_MEM'] || "1024"
  vm_cpu = ENV['HIVE_VM_CPU'] || "1"

  config.vm.provider "virtualbox" do |vb|
    vb.memory = vm_mem
    vb.cpus = vm_cpu
    vb.gui = false 
  end

  config.vm.synced_folder ".", "/vagrant", disabled: true

  config.vm.network "forwarded_port", guest: 22, host: 2222, id: "ssh"
end