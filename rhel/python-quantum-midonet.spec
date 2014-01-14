Name:       python-quantum-midonet
Epoch:      1
Version:    2013.1.2.mido9
Release:    0 
Summary:    OpenStack Midonet plugin
Group:      Applications/System
License:    Test
URL:        https://github.com/midokura/neutron
Source0:    https://github.com/midokura/neutron/python-quantum-midonet-%{version}.tar.gz
BuildArch:  noarch
BuildRoot:  /var/tmp/%{name}-buildroot

%description
This package contains the Quantum Midonet plugin.

%prep
%setup -q 

%install
mkdir -p $RPM_BUILD_ROOT/%{python_sitelib}
mkdir -p $RPM_BUILD_ROOT/%{python_sitelib}/quantum
mkdir -p $RPM_BUILD_ROOT/%{python_sitelib}/quantum/plugins
cp -r quantum/plugins/midonet $RPM_BUILD_ROOT/%{python_sitelib}/quantum/plugins/

%files
%defattr(-,root,root)
%{python_sitelib}/quantum/plugins/midonet

%changelog
* Mon Jan 14 2014 Joe Mills <joe@midokura.com> - 2013.1.2.mido9
* Mon Nov 18 2013 Dave Cahill <dcahill@midokura.com> - 2013.1.2.mido6
* Fri Nov 15 2013 Dave Cahill <dcahill@midokura.com> - 2013.1.2.mido5
* Mon Aug 5 2013 Guillermo Ontanon <guillermo@midokura.com> - 2013.1.2.mido4
* Thu Jul 19 2013 Takaaki Suzuki <suzuki@midokura.com> - 2013.1.2.mido2
- Initial package
