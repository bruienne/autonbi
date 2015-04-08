#!/usr/bin/python
#
# AutoNBI.py - A tool to automate (or not) the building and modifying
#  of Apple NetBoot NBI bundles.
#
# Requirements:
#   * OS X 10.9 Mavericks - This tool relies on parts of the SIUFoundation
#     Framework which is part of System Image Utility, found in
#     /System/Library/CoreServices in Mavericks.
#
#   * Munki tools installed at /usr/local/munki - needed for FoundationPlist.
#
# Thanks to: Greg Neagle for overall inspiration and code snippets (COSXIP)
#            Per Olofsson for the awesome AutoDMG which inspired this tool
#            Tim Sutton for further encouragement and feedback on early versions
#
# This tool aids in the creation of Apple NetBoot Image (NBI) bundles.
# It can run either in interactive mode by passing it a folder, installer
# application or DMG or automatically, integrated into a larger workflow.
#
# Required input is:
#
# * [--source][-s] The valid path to a source of one of the following types:
#
#   - A folder (such as /Applications) which will be searched for one
#     or more valid install sources
#   - An OS X installer application (e.g. "Install OS X Mavericks.app")
#   - An InstallESD.dmg file
#
# * [--destination][-d] The valid path to a dedicated build root folder:
#
#   The build root is where the resulting NBI bundle and temporary build
#   files are written. If the optional --folder arguments is given an
#   identically named folder must be placed in the build root:
#
#   ./AutoNBI <arguments> -d /Users/admin/BuildRoot --folder Packages
#
#   +-> Causes AutoNBI to look for /Users/admin/BuildRoot/Packages
#
# * [--name][-n] The name of the NBI bundle, without .nbi extension
#
# * [--folder] *Optional* The name of a folder to be copied onto
#   NetInstall.dmg. If the folder already exists, it will be overwritten.
#   This allows for the customization of a standard NetInstall image
#   by providing a custom rc.imaging and other required files,
#   such as a custom Runtime executable. For reference, see the
#   DeployStudio Runtime NBI.
#
# * [--auto][-a] Enable automated run. The user will not be prompted for
#   input and the application will attempt to create a valid NBI. If
#   the input source path results in more than one possible installer
#   source the application will stop. If more than one possible installer
#   source is found in interactive mode the user will be presented with
#   a list of possible InstallerESD.dmg choices and asked to pick one.
#
# * [--enable-nbi][-e] Enable the output NBI by default. This sets the "Enabled"
#   key in NBImageInfo.plist to "true".
#
# * [--enable-python][-p] Add the Python framework and libraries to the NBI
#   in order to support Python-based applications at runtime
#
# * [--enable-ruby][-r] Add the Ruby framework and libraries to the NBI
#   in order to support Ruby-based applications at runtime
#
# To invoke AutoNBI in interactive mode:
#   ./AutoNBI -s /Applications -d /Users/admin/BuildRoot -n Mavericks
#
# To invoke AutoNBI in automatic mode:
#   ./AutoNBI -s ~/InstallESD.dmg -d /Users/admin/BuildRoot -n Mavericks -a
#
# To replace "Packages" on the NBI boot volume with a custom version:
#   ./AutoNBI -s ~/InstallESD.dmg -d ~/BuildRoot -n Mavericks -f Packages -a

import os
import sys
import tempfile
import mimetypes
import distutils.core
import subprocess
import plistlib
import optparse
import shutil
from distutils.version import LooseVersion

sys.path.append("/usr/local/munki/munkilib")
import FoundationPlist
from xml.parsers.expat import ExpatError

def _get_mac_ver():
    import subprocess
    p = subprocess.Popen(['sw_vers', '-productVersion'], stdout=subprocess.PIPE)
    stdout, stderr = p.communicate()
    return stdout.strip()

#  Below code from COSXIP by Greg Neagle


def cleanUp():
    """Cleanup our TMPDIR"""
    if TMPDIR:
        shutil.rmtree(TMPDIR, ignore_errors=True)


def fail(errmsg=''):
    """Print any error message to stderr,
    clean up install data, and exit"""
    if errmsg:
        print >> sys.stderr, errmsg
    cleanUp()
    # exit
    exit(1)


def mountdmg(dmgpath, use_shadow=False):
    """
    Attempts to mount the dmg at dmgpath
    and returns a list of mountpoints
    If use_shadow is true, mount image with shadow file
    """
    mountpoints = []
    dmgname = os.path.basename(dmgpath)
    cmd = ['/usr/bin/hdiutil', 'attach', dmgpath,
           '-mountRandom', TMPDIR, '-nobrowse', '-plist',
           '-owners', 'on']
    if use_shadow:
        shadowname = dmgname + '.shadow'
        shadowroot = os.path.dirname(dmgpath)
        shadowpath = os.path.join(shadowroot, shadowname)
        cmd.extend(['-shadow', shadowpath])
    else:
        shadowpath = None
    proc = subprocess.Popen(cmd, bufsize=-1,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (pliststr, err) = proc.communicate()
    if proc.returncode:
        print >> sys.stderr, 'Error: "%s" while mounting %s.' % (err, dmgname)
    if pliststr:
        plist = plistlib.readPlistFromString(pliststr)
        for entity in plist['system-entities']:
            if 'mount-point' in entity:
                mountpoints.append(entity['mount-point'])

    return mountpoints, shadowpath


def unmountdmg(mountpoint):
    """
    Unmounts the dmg at mountpoint
    """
    proc = subprocess.Popen(['/usr/bin/hdiutil', 'detach', mountpoint],
                            bufsize=-1, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    (unused_output, err) = proc.communicate()
    if proc.returncode:
        print >> sys.stderr, 'Polite unmount failed: %s' % err
        print >> sys.stderr, 'Attempting to force unmount %s' % mountpoint
        # try forcing the unmount
        retcode = subprocess.call(['/usr/bin/hdiutil', 'detach', mountpoint,
                                   '-force'])
        print('Unmounting successful...')
        if retcode:
            print >> sys.stderr, 'Failed to unmount %s' % mountpoint

#  Above code from COSXIP by Greg Neagle


def convertdmg(dmgpath, nbishadow):
    """
    Converts the dmg at mountpoint to a .sparseimage
    """
    # Get the full path to the DMG minus the extension, hdiutil adds one
    dmgfinal = os.path.splitext(dmgpath)[0]

    # Run a basic 'hdiutil convert' using the shadow file to pick up
    #   any changes we made without needing to convert between r/o and r/w
    cmd = ['/usr/bin/hdiutil', 'convert', dmgpath, '-format', 'UDSP',
           '-shadow', nbishadow, '-o', dmgfinal]
    proc = subprocess.Popen(cmd, bufsize=-1,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (unused, err) = proc.communicate()

    # Got errors?
    if proc.returncode:
        print >> sys.stderr, 'Disk image conversion failed: %s' % err

    # Return the name of the converted DMG back to the caller
    return dmgfinal + '.sparseimage'


def getosversioninfo(mountpoint):
    """"getosversioninfo will attempt to retrieve the OS X version and build
        from the given mount point by reading /S/L/CS/SystemVersion.plist
        Most of the code comes from COSXIP without changes."""

    # Check for availability of BaseSystem.dmg
    basesystem_dmg = os.path.join(mountpoint, 'BaseSystem.dmg')
    if not os.path.isfile(basesystem_dmg):
        unmountdmg(mountpoint)
        fail('Missing BaseSystem.dmg in %s' % mountpoint)

    # Mount BaseSystem.dmg
    basesystemmountpoints, unused_shadowpath = mountdmg(basesystem_dmg)
    basesystemmountpoint = basesystemmountpoints[0]

    # Read SystemVersion.plist from the mounted BaseSystem.dmg
    system_version_plist = os.path.join(
        basesystemmountpoint,
        'System/Library/CoreServices/SystemVersion.plist')
    # Now parse the .plist file
    try:
        version_info = plistlib.readPlist(system_version_plist)

    # Got errors?
    except (ExpatError, IOError), err:
        unmountdmg(basesystemmountpoint)
        unmountdmg(mountpoint)
        fail('Could not read %s: %s' % (system_version_plist, err))

    # Done, unmount BaseSystem.dmg
    else:
        unmountdmg(basesystemmountpoint)

    # Return the version and build as found in the parsed plist
    return version_info.get('ProductUserVisibleVersion'), \
           version_info.get('ProductBuildVersion'), mountpoint


def buildplist(nbiindex, nbidescription, nbiname, nbienabled, destdir=__file__):
    """buildplist takes a source, destination and name parameter that are used
        to create a valid plist for imagetool ingestion."""

    nbipath = os.path.join(destdir, nbiname + '.nbi')
    platformsupport = FoundationPlist.readPlist(os.path.join(nbipath, 'i386', 'PlatformSupport.plist'))
    enabledsystems = platformsupport.get('SupportedModelProperties')

    nbimageinfo = {'IsInstall': True,
                   'Index': nbiindex,
                   'Kind': 1,
                   'Description': nbidescription,
                   'Language': 'Default',
                   'IsEnabled': nbienabled,
                   'SupportsDiskless': False,
                   'RootPath': 'NetInstall.dmg',
                   'EnabledSystemIdentifiers': enabledsystems,
                   'BootFile': 'booter',
                   'Architectures': ['i386'],
                   'BackwardCompatible': False,
                   'DisabledSystemIdentifiers': [],
                   'Type': 'NFS',
                   'IsDefault': False,
                   'Name': nbiname,
                   'osVersion': '10.9'}

    plistfile = os.path.join(nbipath, 'NBImageInfo.plist')
    FoundationPlist.writePlist(nbimageinfo, plistfile)


def locateinstaller(rootpath='/Applications', auto=False):
    """locateinstaller will process the provided root path and looks for
        potential OS X installer apps containing InstallESD.dmg. Runs
        in interactive mode by default unless '-a' was provided at run"""

    # The given path doesn't exist, bail
    if not os.path.exists(rootpath):
        print "The root path '" + rootpath + "' is not a valid path - unable " \
                                             "to proceed."
        sys.exit(1)

    # Auto mode specified but the root path is not the installer app, bail
    elif auto and not rootpath.endswith('.app'):
        print 'Mode is auto but the rootpath is not an installer app or DMG, ' \
              ' unable to proceed.'
        sys.exit(1)

    # We're auto and the root path is an app - check InstallESD.dmg is there
    #   and return its location.
    elif auto and rootpath.endswith('.app'):
        # Now look for the DMG
        if os.path.exists(os.path.join(rootpath, 'Contents/SharedSupport/InstallESD.dmg')):
            return os.path.join(rootpath, 'Contents/SharedSupport/InstallESD.dmg')
        else:
            print 'Unable to locate InstallESD.dmg in ' + rootpath + ' - exiting.'
            sys.exit(1)

    # Lastly, if we're running interactively we construct a list of possible
    #   installer apps.
    elif not auto:
        # Initialize an empty list to store all found OS X installer apps
        installers = []

        # List the contents of the given root path
        for item in os.listdir(rootpath):

            # Look for any OS X installer apps
            if item.startswith('Install OS X'):

                # If an potential installer app was found, look for the DMG
                for d, p, files in os.walk(os.path.join(rootpath, item)):
                    for file in files:

                        # Excelsior! An InstallESD.dmg was found. Add it it
                        #   to the installers list
                        if file.endswith('InstallESD.dmg'):
                            installers.append(os.path.join(rootpath, item))

        # If the installers list has no contents no installers were found, bail
        if len(installers) == 0:
            print 'No suitable installers found in ' + rootpath + \
                  ' - unable to proceed.'
            sys.exit(1)

        # One or more installers were found, return the list to the caller
        else:
            return installers


def pickinstaller(installers):
    """pickinstaller provides an interactive picker when more than one
        potential OS X installer app was returned by locateinstaller() """

    # Initialize choice
    choice = ''

    # Cycle through the installers and print an enumerated list to stdout
    for item in enumerate(installers):
        print "[%d] %s" % item

    # Have the user pick an installer
    try:
        idx = int(raw_input("Pick installer to use: "))

    # Got errors? Not a number, bail.
    except ValueError:
        print "Not a valid selection - unable to proceed."
        sys.exit(1)

    # Attempt to pull the installer using the user's input
    try:
        choice = installers[idx]

    # Got errors? Not a valid index in the list, bail.
    except IndexError:
        print "Not a valid selection - unable to proceed."
        sys.exit(1)

    # We're done, return the user choice to the caller
    return choice


def createnbi(workdir, description, name, enabled, dmgmount):
    """createnbi calls the 'createNetInstall.sh' script with the
        environment variables from the createvariables dict."""

    # Setup the path to our executable and pass it the CLI arguments
    # it expects to get: build root and DMG size. We use 7 GB to be safe.
    buildexec = os.path.join(BUILDEXECPATH, 'createNetInstall.sh')
    cmd = [buildexec, workdir, '7000']

    destpath = os.path.join(workdir, name + '.nbi')
    createvariables = {'destPath': destpath,
                       'dmgTarget': 'NetInstall',
                       'dmgVolName': name,
                       'destVolFSType': 'JHFS+',
                       'installSource': dmgmount,
                       'scriptsDebugKey': 'INFO',
                       'ownershipInfoKey': 'root:wheel'}
    proc = subprocess.Popen(cmd, bufsize=-1, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=createvariables)

    (unused, err) = proc.communicate()

    # Got errors? Bail.
    if proc.returncode:
        print >> sys.stderr, 'Error: "%s" while processing %s.' % (err, unused)
        sys.exit(1)

    buildplist(5000, description, name, enabled, workdir)

    os.unlink(os.path.join(workdir, 'createCommon.sh'))
    os.unlink(os.path.join(workdir, 'createVariables.sh'))


def prepworkdir(workdir):
    """Copies in the required Apple-provided createCommon.sh and also creates
        an empty file named createVariables.sh. We actually pass the variables
        this file might contain using environment variables but it is expected
        to be present so we fake out Apple's createNetInstall.sh script."""

    commonsource = os.path.join(BUILDEXECPATH, 'createCommon.sh')
    commontarget = os.path.join(workdir, 'createCommon.sh')

    shutil.copyfile(commonsource, commontarget)
    open(os.path.join(workdir, 'createVariables.sh'), 'a').close()


class processNBI(object):
    """The processNBI class provides the makerw(), modify() and close()
        functions. All functions serve to make modifications to an NBI
        created by createnbi()"""

    # Don't think we need this.
    def __init__(self, customfolder = None, enablepython=False, enableruby=False):
         super(processNBI, self).__init__()
         self.customfolder = customfolder
         self.enablepython = enablepython
         self.enableruby = enableruby
         self.hdiutil = '/usr/bin/hdiutil'


    # Make the provided NetInstall.dmg r/w by mounting it with a shadow file
    def makerw(self, netinstallpath):
        # Call mountdmg() with the use_shadow option set to True
        nbimount, nbishadow = mountdmg(netinstallpath, use_shadow=True)

        # Send the mountpoint and shadow file back to the caller
        return nbimount[0], nbishadow

    # Handle the addition of system frameworks like Python and Ruby using the
    #   OS X installer source
    # def enableframeworks(self, source, shadow):

    def dmgattach(self, attach_source, shadow_file):
        return [ self.hdiutil, 'attach',
                               '-shadow', shadow_file,
                               '-mountRandom', TMPDIR,
                               '-nobrowse',
                               '-plist',
                               '-owners', 'on',
                               attach_source ]
    def dmgdetach(self, detach_mountpoint):
        return [ self.hdiutil, 'detach',
                          detach_mountpoint ]
    def dmgconvert(self, convert_source, convert_target, shadow_file):
        return [ self.hdiutil, 'convert',
                          '-format', 'UDRO',
                          '-o', convert_target,
                          '-shadow', shadow_file,
                          convert_source ]
    def dmgresize(self, resize_source, shadow_file):
        return [ self.hdiutil, 'resize',
                          '-size', '10G',
                          '-shadow', shadow_file,
                          resize_source ]
    def xarextract(self, xar_source):
        return [ '/usr/bin/xar', '-x',
                                 '-f', xar_source,
                                 'Payload',
                                 '-C', TMPDIR ]
    def cpioextract(self, cpio_source):
        return [ '/usr/bin/cpio -idmu --quiet -I %s \"*Py*\" \"*py*\"' % cpio_source ]
    def getfiletype(self, filepath):
        return ['/usr/bin/file', filepath]

    def runcmd(self, cmd, cwd=None):

        print cmd

        if type(cwd) is not str:
            proc = subprocess.Popen(cmd, bufsize=-1,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (result, err) = proc.communicate()
        else:
            proc = subprocess.Popen(cmd, bufsize=-1,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            shell=True)
            (result, err) = proc.communicate()

        if proc.returncode:
            print >> sys.stderr, 'Error "%s" while running command %s' % (err, cmd)

        return result

    # Code for parse_pbzx from https://gist.github.com/pudquick/ac29c8c19432f2d200d4
    def parse_pbzx(self, pbzx_path, xar_out_path):
        import struct
        f = open(pbzx_path, 'rb')
        pbzx = f.read()
        f.close()
        magic, pbzx = pbzx[:4],pbzx[4:]
        if magic != 'pbzx':
            raise "Error: Not a pbzx file"
        # Read 8 bytes for initial flags
        flags, pbzx = pbzx[:8],pbzx[8:]
        # Interpret the flags as a 64-bit big-endian unsigned int
        flags = struct.unpack('>Q', flags)[0]
        xar_f = open(xar_out_path, 'wb')
        while (flags & (1 << 24)):
            # Read in more flags
            flags, pbzx = pbzx[:8],pbzx[8:]
            flags = struct.unpack('>Q', flags)[0]
            # Read in length
            f_length, pbzx = pbzx[:8],pbzx[8:]
            f_length = struct.unpack('>Q', f_length)[0]
            xzmagic, pbzx = pbzx[:6],pbzx[6:]
            if xzmagic != '\xfd7zXZ\x00':
                xar_f.close()
                raise "Error: Header is not xar file header"
            f_length -= 6
            f_content, pbzx = pbzx[:f_length],pbzx[f_length:]
            if f_content[-2:] != 'YZ':
                xar_f.close()
                raise "Error: Footer is not xar file footer"
            xar_f.write(xzmagic)
            xar_f.write(f_content)
        try:
            xar_f.close()
        except:
            pass


    # Allows modifications to be made to a DMG previously made writable by
    #   processNBI.makerw()
    def modify(self, nbimount, dmgpath, nbishadow, installersource):
        # DO STUFF
        if self.customfolder is not None:
            print "Modifying NetBoot volume at %s" % nbimount

            # Sets up which directory to process. This is a simple version until
            # we implement something more full-fledged, based on a config file
            # or other user-specified source of modifications.
            processdir = os.path.join(nbimount, ''.join(self.customfolder.split('/')[-1:]))

            # Remove folder being modified - distutils appears to have the easiest
            # method to recursively delete a folder. Same with recursively copying
            # back its replacement.
            print('About to process ' + processdir + ' for replacement...')
            if os.path.exists(processdir):
                distutils.dir_util.remove_tree(processdir)
                os.mkdir(processdir)
                print('Copying ' + self.customfolder + ' to ' + processdir + '...')
                distutils.dir_util.copy_tree(self.customfolder, processdir)
        if self.enablepython:

            print("Adding Python framework from %s to NBI at %s" % (installersource, nbimount))

            # Extract Payload from desired OS X installer package
            xar_source = os.path.join(installersource, 'Packages', 'BSD.pkg')
            result = self.runcmd(self.xarextract(xar_source))

            payloadsource = os.path.join(TMPDIR, 'Payload')
            payloadtype = self.runcmd(self.getfiletype(payloadsource)).split(': ')[1]

            cpio_source = os.path.join(TMPDIR, 'Payload.cpio.xz')

            # Check filetype of the Payload, 10.10 adds a pbzx wrapper
            if payloadtype.startswith('data'):
                # This is most likely pbzx-wrapped, unwrap it
                self.parse_pbzx(payloadsource, cpio_source)
                os.remove(payloadsource)
            else:
                # No pbzx wrapper, rename and move to cpio extraction
                os.rename(payloadsource, cpio_source)

            # Extract all or some (using shell globbing pattern) files from CPIO archive
            self.runcmd(self.cpioextract(cpio_source), cwd=nbimount)
            os.remove(cpio_source)

        if self.enableruby:
            #do stuff
            print('Ruby not ready yet')
            pass

        # We're done, unmount the DMG.
        unmountdmg(nbimount)

        # Convert modified DMG to .sparseimage, this will shrink the image
        # automatically after modification.
        print "Sealing DMG at path %s using shadow file %s" % (dmgpath,
                                                               nbishadow)
        dmgfinal = convertdmg(dmgpath, nbishadow)
        # print('Got back final DMG as ' + dmgfinal + ' from convertdmg()...')

        # Do some cleanup, remove original DMG, its shadow file and rename
        # .sparseimage to NetInstall.dmg
        os.remove(nbishadow)
        os.remove(dmgpath)
        os.rename(dmgfinal, dmgpath)

        # else:
        #     # We're done, unmount the DMG.
        #     print('customfolder was None, skipping modification...')
        #     unmountdmg(nbimount)
        #     os.remove(nbishadow)

TMPDIR = None

print _get_mac_ver()

if LooseVersion(_get_mac_ver()) < "10.10":
    BUILDEXECPATH = ('/System/Library/CoreServices/System Image Utility.app/Contents/Frameworks/SIUFoundation.framework/'
                 'Versions/A/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')
else:
    BUILDEXECPATH = ('/System/Library/CoreServices/Applications/System Image Utility.app/Contents/Frameworks/SIUFoundation.framework/'
                 'Versions/A/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')


def main():
    """Main routine"""

    global TMPDIR

    # TBD - Full usage text
    usage = ('Usage: %prog --source/-s <path>\n'
             '                   --destination/-d <path>\n'
             '                   --name/-n MyNBI\n'
             '                   [--folder/-f] FolderName\n'
             '                   [--auto/-a]\n'
             '    %prog creates a Lion, Mountain Lion or Mavericks NetBoot NBI\n'
             '    ready for use with a NetBoot server.\n\n'
             '    An option to modify the NBI\'s NetInstall.dmg is also provided\n'
             '    by specifying an optional name of a folder in the source root\n'
             '    to add or replace on the NetInstall.dmg.\n\n'
             '    Examples:\n'
             '    ./AutoNBI.py -s /Applications -d ~/BuildRoot -n MyNBI\n'
             '    ./AutoNBI.py -s /Volumes/Disk/Install OS X Mavericks.app -d'
             ' ~/BuildRoot -n MyNBI -a\n'
             '    ./AutoNBI.py -s ~/Documents/InstallESD.dmg -d ~/BuildRoot -n MyNBI'
             ' -f Packages -a')

    # Setup a parser instance
    parser = optparse.OptionParser(usage=usage)

    # Setup the recognized options
    parser.add_option('--source', '-s',
                      help='Required. Path to Install Mac OS X Lion.app '
                           'or Install OS X Mountain Lion.app or Install OS X Mavericks.app')
    parser.add_option('--destination', '-d',
                      help='Required. Path to save .plist and .nbi files')
    parser.add_option('--name', '-n',
                      help='Required. Name of the NBI, also applies to .plist')
    parser.add_option('--folder', '-f', default='',
                      help='Optional. Name of a folder on the NBI to modify. This will be the\
                            root below which changes will be made')
    parser.add_option('--auto', '-a', action='store_true', default=False,
                      help='Optional. Toggles automation mode, suitable for scripted runs')
    parser.add_option('--enable-nbi', '-e', action='store_true', default=False,
                      help='Optional. Enables NBI.', dest='enablenbi')
    parser.add_option('--add-ruby', '-r', action='store_true', default=False,
                      help='Optional. Enables Ruby in BaseSystem.', dest='addruby')
    parser.add_option('--add-python', '-p', action='store_true', default=False,
                      help='Optional. Enables Python in BaseSystem.', dest='addpython')

    # Parse the provided options
    options, arguments = parser.parse_args()

    # Are we root?
    if os.getuid() != 0:
        parser.print_usage()
        print >> sys.stderr, 'This tool requires sudo or root privileges.'
        exit(-1)

    # Setup our base requirements for installer app root path, destination,
    #   name of the NBI and auto mode.
    root = options.source
    destination = options.destination
    name = options.name
    auto = options.auto
    enablenbi = options.enablenbi
    customfolder = options.folder
    addpython = options.addpython
    addruby = options.addruby

    # Set 'modifydmg' if any of 'addcustom', 'addpython' or 'addruby' are set
    addcustom = len(customfolder) > 0
    modifynbi = (addcustom or addpython or addruby)

    # Spin up a tmp dir for mounting
    TMPDIR = tempfile.mkdtemp(dir=TMPDIR)

    # If the destination path isn't absolute, we make it so to prevent errors
    if not destination.startswith('/'):
        destination = os.path.abspath(destination)

    # Now we start a typical run of the tool, first locate one or more
    #   installer app candidates

    if os.path.isdir(root):
        print 'Locating installer...'
        source = locateinstaller(root, auto)
    elif mimetypes.guess_type(root)[0].endswith('diskimage'):
        print 'Source is a disk image.'
        source = root
    else:
        print 'Source is neither an installer app or InstallESD.dmg.'
        sys.exit(-1)

    # If we have a list for our source, more than one installer app was found
    #   so we run the list through pickinstaller() to pick one interactively
    if type(source) == list:
        source = pickinstaller(source)

    print 'Creating NBI... (this may take a while)'

    # Prep the build root - create it if it's not there
    if not os.path.exists(destination):
        os.mkdir(destination)

    # Mount our installer source DMG
    print 'Mounting ' + source
    mountpoints = mountdmg(source)

    # Get the mount point for the DMG
    if len(mountpoints) > 1:
        for i in mountpoints[0]:
            if i.find('dmg'):
                mount = i
    else:
        mount = mountpoints[0]

    osversion, osbuild, unused = getosversioninfo(mount)
    description = 'OS X ' + osversion + '-' + osbuild

    # Prep our build root for NBI creation
    print 'Prepping ' + destination + ' with source mounted at ' + mount
    prepworkdir(destination)

    # Now move on to the actual NBI creation
    print 'Creating NBI at ' + destination
    createnbi(destination, description, name, enablenbi, mount)

    # Make our modifications if any were provided from the CLI
    if modifynbi:
        if addcustom:
            try:
                if os.path.isdir(customfolder):
                    customfolder = os.path.abspath(customfolder)
            except IOError:
                print customfolder + " is not a valid path - unable to proceed."
                sys.exit(1)

        # Path to the NetInstall.dmg
        netinstallpath = os.path.join(destination, name + '.nbi', 'NetInstall.dmg')

        # Initialize a new processNBI() instance as 'nbi'
        nbi = processNBI(customfolder, addpython, addruby)

        # Run makerw() to enable modifications
        nbimount, nbishadow = nbi.makerw(netinstallpath)

        nbi.modify(nbimount, netinstallpath, nbishadow, mount)

        # We're done, unmount all the things
        unmountdmg(mount)
        distutils.dir_util.remove_tree(TMPDIR)

        print 'Modifications complete...'
        print 'Done.'
    else:
        # We're done, unmount all the things
        unmountdmg(mount)
        distutils.dir_util.remove_tree(TMPDIR)

        print 'No modifications will be made...'
        print 'Done.'

if __name__ == '__main__':
    main()
