#!/usr/bin/python
#
# buildNBI.py - A tool to automate (or not) the building and modifying
#  of Apple NetBoot NBI bundles
#
# Requirements:
#   - OS X 10.9 Mavericks - This tool relies on the 'imagetool'
#       which AFAIK didn't ship prior to OS X 10.9.
#   - Munki tools installed at /usr/local/munki - needed for FoundationPlist.
#
# This tool allows the user to bypass System Image Utility for creating a valid
#  plist for use with the 'imagetool --plist' invocation. After a valid plist
#  is created the tool then calls imagetool --plist <plist path> and a new
#  NBI bundle will be created at the location specified by the user.
#

import os
import sys
import string
import random
import tempfile

sys.path.append("/usr/local/munki/munkilib")
import FoundationPlist

import subprocess
import plistlib
import optparse

from xml.dom import minidom
from xml.parsers.expat import ExpatError

##### BEGIN ############################
#  Below code from COSXIP by Greg Neagle

def cleanUp():
    '''Cleanup our TMPDIR'''
    if TMPDIR:
        shutil.rmtree(TMPDIR, ignore_errors=True)

def fail(errmsg=''):
    '''Print any error message to stderr,
    clean up install data, and exit'''
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
        if retcode:
            print >> sys.stderr, 'Failed to unmount %s' % mountpoint

#  Above code from COSXIP by Greg Neagle
##### END ##############################

def convertdmg(dmgpath, nbishadow):
    """
    Converts the dmg at mountpoint to .sparseimage
    """
    # Get the full path to the DMG minus the extension, hdiutil adds one
    dmgfinal = os.path.splitext(dmgpath)[0]
    
    # Run a basic 'hdiutil convert' using the shadow file to pick up
    #   any changes we made without needing to convert between r/o and r/w
    cmd = ['/usr/bin/hdiutil', 'convert', dmgpath,'-format', 'UDSP',
        '-shadow', nbishadow,'-o', dmgfinal]
    proc = subprocess.Popen(cmd, bufsize=-1,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (unused, err) = proc.communicate()
    
    # Got errors?
    if proc.returncode:
        print >> sys.stderr, 'Disk image conversion failed: %s' % err
    
    # Return the name of the converted DMG back to the caller
    return dmgfinal + '.sparseimage'

# Most likely to be removed, converting to UDSP format takes care of resizing
#   the DMG based on the removal of any of its contents.
#
# def resizedmg(dmgpath):
#     """
#     Resizes the dmg at mountpoint
#     """
#     print dmgpath
#     cmd = ['/usr/bin/hdiutil', 'compact', dmgpath]
#     # cmd = ['/usr/bin/hdiutil', 'resize', '750g', '-imageonly', dmgpath]
#     print cmd
#     proc = subprocess.Popen(cmd, bufsize=-1,
#         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
#     (unused_output, err) = proc.communicate()
#     if proc.returncode:
#         print >> sys.stderr, 'Disk image resizing failed: %s' % err

def getosversioninfo(mountpoint):
    """"getosversioninfo will attempt to retrieve the OS X version and build
        from the given mount point by reading /S/L/CS/SystemVersion.plist
        Most of the code comes from COSXIP without changes."""
    
    # Check for availability of BaseSystem.dmg
    basesystem_dmg = os.path.join(mountpoint, 'BaseSystem.dmg')
    if not os.path.isfile(basesystem_dmg):
        unmountdmg(mountpoint)
        fail('Missing BaseSystem.dmg in %s'% source)
    
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
    return version_info.get('ProductUserVisibleVersion'), version_info.get('ProductBuildVersion')

def buildplist(source = '', destdir = __file__, name = ''):
    """buildplist takes a source, destination and name parameter that are used
        to create a valid plist for imagetool ingestion."""

    # Set variables for the output plist, path to the source DMG inside the
    #  app bundle and the NBI's index

    # If the user didn't supply a destination we default to the cwd of
    #   the script.
    if destdir == __file__:
        destdir = os.path.dirname(destdir)

    # Construct the path to the InstallESD.dmg
    dmgpath = os.path.join(source, 'Contents/SharedSupport/InstallESD.dmg')

    os_version = None
    os_build = None

    # Now mount InstallESD.dmg
    mountpoints = mountdmg(dmgpath)
    
    # Get the mountpoint for the DMG
    for mount in mountpoints[0]:
        if mount.find('dmg'):
            os_version, os_build = getosversioninfo(mount)
        unmountdmg(mount)

    # Setup some variables for the name and full path of the .plist and NBI
    baselocation = os.path.join(destdir , name)
    build_version = '_' + os_version + '_' + os_build
    plistfile = os.path.join(baselocation + build_version + '.plist')
    nbilocation = baselocation
    
    # The NBI index
    index = 5000 # TBD: figure out a way to keep track of previous idxs

    # Initialize an empty dict that will hold the plist contents
    nbiconfig = {}
    nbiconfig['automatedInstall'] = \
                {'eraseTarget': True, \
                 'language': 'en', \
                 'targetType': True, \
                 'targetVolume': 'Macintosh HD'}
    nbiconfig['sourcesList'] = \
                [{'dmgPath': dmgpath, \
                  'isInstallMedia': True, \
                  'kindOfSource': 1, \
                  'sourceType': 'ESDApplication', \
                  'volumePath': source}]
    nbiconfig['imageDescription'] = 'Auto-build of ' + name
    nbiconfig['imageIndex'] = index
    nbiconfig['imageName'] = name + build_version
    nbiconfig['installType'] = 'netinstall'
    nbiconfig['nbiLocation'] = nbilocation + build_version

    # Write out the now-complete dict as a standard plist to our previously
    #  configured destination file
    FoundationPlist.writePlist(nbiconfig, plistfile)

    # Return the path to the configuration plist to the caller
    return plistfile, str(nbiconfig['nbiLocation'])
    # return nbiconfig

def locateinstaller(rootpath = '/Applications', auto = False):
    """locateinstaller will process the provided root path and looks for
        potential OS X installer apps containing InstallESD.dmg. Runs
        in interactive mode by default unless '-a' was provided at run"""

    # The given path doesn't exist, bail
    if not os.path.exists(rootpath):
        print "The root path '" + rootpath + "' is not a valid path - unable "\
                "to proceed."
        sys.exit(1)
    # elif auto and rootpath == '':
    #     print 'Mode is auto but no rootpath was given, unable to proceed.'
    #     sys.exit(1)

    # Auto mode specified but the root path is not the installer app, bail
    elif auto and not rootpath.endswith('.app'):
        print 'Mode is auto but the rootpath is not an installer app, unable '\
                'to proceed.'
        sys.exit(1)

    # We're auto and the root path is an app - proceed
    elif auto and rootpath.endswith('.app'):
        return rootpath
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

def createnbi(plist):
    """createnbi calls the 'imagetool' binary with the --plist option
        and the .plist file we created with buildplist()"""

    # Setup the cmd and options
    cmd = ['/System/Library/CoreServices/System Image Utility.app/Contents/MacOS/imagetool', '--plist', plist]
    proc = subprocess.Popen(cmd, bufsize=-1, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    (unused, err) = proc.communicate()
    
    # Got errors? Bail.
    if proc.returncode:
        print >> sys.stderr, 'Error: "%s" while processing %s.' % (err, plist)
        sys.exit(1)

class processNBI(object):
    """The processNBI class provides the makerw(), modify() and close()
        functions. All functions serve to make modifications to an NBI
        created by createnbi()"""
    
    # Don't think we need this.
    # def __init__(self, arg):
    #     super(processNBI, self).__init__()
    #     self.arg = arg
    
    # Make the provided NetInstall.dmg r/w by mounting it with a shadow file
    def makerw(self, netinstallpath):
        
        # Call mountdmg() with the use_shadow option set to True
        nbimount, nbishadow = mountdmg(netinstallpath, use_shadow=True)
        
        # Send the mountpoint and shadow file back to the caller
        return nbimount[0], nbishadow
    
    # Allows modifications to be made to a DMG previously made writable by
    #   processNBI.makerw()
    def modify(self, nbimount, items = None):
        
        # DO STUFF
        print "Doing stuff with shadowed DMG at path %s" % nbimount
        
        # Sets up which directory to process. This is a simple stub until
        #   we implement something more full-fledged, based on a config file
        #   or other user-specified source of modifications.
        processdir = os.path.join(nbimount, 'Packages')
        
        # Recursive clearing out of a directory, probably will move this into
        #   its own function once a complete mod-config is implemented.
        for root, dirs, files in os.walk(processdir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        
        # Python version of 'touch' - testing purposes.
        # open(os.path.join(processdir, 'testing'), 'a').close()

        # We're done, unmount the DMG.
        unmountdmg(nbimount)

    # Convert modified DMG to .sparseimage, this will shrink its size
    #   automatically if items were deleted during modification.
    def close(self, dmgpath, nbishadow):
        print "Sealing DMG at path %s using shadow file %s" % (dmgpath,
            nbishadow)
        dmgfinal = convertdmg(dmgpath, nbishadow)
        
        # Do some cleanup, remove original DMG, its shadow file and symlink
        #   .sparseimage to NetInstall.dmg (DS style)
        os.remove(nbishadow)
        os.remove(dmgpath)
        os.symlink(dmgfinal, dmgpath)

TMPDIR = None
def main():
    """docstring for main"""

    global TMPDIR

    # TBD - Full usage text
    usage = ('Usage: %prog --source <path>\n'
        '                   --destination <path>\n'
        '                   --name MyNBI\n'
        '                   [--auto]\n'
        '    %prog creates a Lion, Mountain Lion or Mavericks NetBoot NBI\n'
        '    ready for use with a NetBoot server.\n\n'
        '    An option to modify the NBI\'s NetInstall.dmg is also provided.\n'
        '\n'
        '    Example:\n'
        '    ./buildNBI.py -s /Applications -d ~/Documents -n MyNBI\n'
        '    ./buildNBI.py -s /Volumes/Disk/Install OS X Mavericks.app -d ~/Documents -n MyNBI -a')

    # Setup a parser instance
    parser = optparse.OptionParser(usage = usage)
    
    # Setup the recognized options
    parser.add_option('--source', '-s',
        help='Required. Path to Install Mac OS X Lion.app '
        'or Install OS X Mountain Lion.app or Install OS X Mavericks.app')
    parser.add_option('--destination', '-d',
        help='Required. Path to save .plist and .nbi files')
    parser.add_option('--name', '-n',
        help='Required. Name of the NBI, also applies to .plist')
    parser.add_option('--auto', '-a', action='store_true', default=False,
        help='Optional. Toggles automation mode, suitable for scripted runs')

    # Parse the provided options
    options, arguments = parser.parse_args()

    if os.getuid() != 0:
        parser.print_usage()
        print >> sys.stderr, 'This tool requires sudo or root access.'
        exit(-1)

    # Setup our base requirements for installer app root path, destination,
    #   name of the NBI and auto mode.
    root = options.source
    destination = options.destination
    name = options.name
    auto = options.auto

    # Spin up a tmp dir for mounting
    TMPDIR = tempfile.mkdtemp(dir=TMPDIR)

    # If the destination path isn't absolute, we make it so to prevent errors
    if not destination.startswith('/'):
        destination = os.path.abspath(destination)

    # Now we start a typical run of the tool, first locate one or more
    #   installer app candidates
    print 'Locating installer...'
    source = locateinstaller(root, auto)

    # We need to generate the .plist for imagetool but first we need to
    #   ensure we've got a valid source
    print 'Generating plist...'
    
    # If we have a list for our source, more than one installer app was found
    #   so run the list through pickinstaller() interactively
    if type(source) == list:
        choice = pickinstaller(source)

        # Now run buildplist() with our choice, destination and NBI name
        plistfile, nbiLocation = buildplist(choice, destination, name)
    else:
        # Now run buildplist() with our choice, destination and NBI name
        plistfile, nbiLocation = buildplist(source, destination, name)

    # Now move on to the actual NBI creation, we pass it the plistfile var
    print 'Creating NBI... (this may take a while)'
    createnbi(plistfile)
    
    # Path to the NetInstall.dmg
    netinstallpath = os.path.join(nbiLocation + '.nbi', 'NetInstall.dmg')
    
    # Initialize a new processNBI() instance as 'nbi'
    nbi = processNBI()

    # Run makerw() to enable modifications
    nbimount, nbishadow = nbi.makerw(netinstallpath)

    # Make our modifications
    nbi.modify(nbimount)

    # Close up the modified image and we're done. Huzzah!
    nbi.close(netinstallpath, nbishadow)
    
if __name__ == '__main__':
    main()
