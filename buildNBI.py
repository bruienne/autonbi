#!/usr/bin/python
#
# buildNBI.py - A tool to automate (or not) the building and modifying
#  of Apple NetBoot NBI bundles
#
# Requirements: OS X 10.9 Mavericks - This tool relies on the 'imagetool'
#  which AFAIK didn't ship prior to OS X 10.9
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
        shadowpath = os.path.join(TMPDIR, shadowname)
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

def getOSversionInfo(mountpoint):
    # get info from BaseSystem.dmg
    basesystem_dmg = os.path.join(mountpoint, 'BaseSystem.dmg')
    if not os.path.isfile(basesystem_dmg):
        unmountdmg(mountpoint)
        fail('Missing BaseSystem.dmg in %s'% source)

    basesystemmountpoints, unused_shadowpath = mountdmg(basesystem_dmg)
    basesystemmountpoint = basesystemmountpoints[0]
    system_version_plist = os.path.join(
        basesystemmountpoint,
        'System/Library/CoreServices/SystemVersion.plist')
    try:
        version_info = plistlib.readPlist(system_version_plist)
    except (ExpatError, IOError), err:
        unmountdmg(basesystemmountpoint)
        unmountdmg(mountpoint)
        fail('Could not read %s: %s' % (system_version_plist, err))
    else:
        unmountdmg(basesystemmountpoint)

    return version_info.get('ProductUserVisibleVersion'), version_info.get('ProductBuildVersion')

def buildPlist(source = '', dest = __file__, name = ''):
    """buildPlist takes a source, destination and name parameter that are used
        to create a valid plist for imagetool ingestion."""

    # Set variables for the output plist, path to the source DMG inside the
    #  app bundle and the NBI's index
    if dest == __file__:
        destdir = os.path.dirname(dest)
    else:
        destdir = dest

    dmgpath = os.path.join(source, 'Contents/SharedSupport/InstallESD.dmg')

    os_version = None
    os_build = None

    mountpoints = mountdmg(dmgpath)
    for mount in mountpoints[0]:
        if mount.find('dmg'):
            os_version, os_build = getOSversionInfo(mount)
            # print os_version, os_build
        unmountdmg(mount)

    # randomize = '_' + "".join([random.choice(string.ascii_uppercase) for x in xrange(8)])

    baselocation = os.path.join(destdir , name)
    # plistfile = baselocation + '_' + randomize + '.plist'
    build_version = '_' + os_version + '_' + os_build
    plistfile = os.path.join(baselocation + build_version + '.plist')
    nbilocation = baselocation

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
    return plistfile
    # return nbiconfig

def locateInstaller(rootpath = '/Applications', auto = False):
    """docstring for locateInstaller"""

    if not os.path.exists(rootpath):
        print "The root path '" + rootpath + "' is not a valid path - unable "\
                "to proceed."
        sys.exit(1)
    # elif auto and rootpath == '':
    #     print 'Mode is auto but no rootpath was given, unable to proceed.'
    #     sys.exit(1)
    elif auto and not rootpath.endswith('.app'):
        print 'Mode is auto but the rootpath is not an installer app, unable '\
                'to proceed.'
        sys.exit(1)
    elif auto and rootpath.endswith('.app'):
        return rootpath
    elif not auto:
        # Initialize an empty list to store all found OS X installer apps
        installers = []
        for item in os.listdir(rootpath):
            if item.startswith('Install OS X'):
                for d, p, files in os.walk(os.path.join(rootpath, item)):
                    for file in files:
                        if file.endswith('InstallESD.dmg'):
                            installers.append(os.path.join(rootpath, item))
        if len(installers) == 0:
            print 'No suitable installers found in ' + rootpath + \
                    ' - unable to proceed.'
            sys.exit(1)
        else:
            return installers

def pickInstaller(installers):
    """docstring for pickInstaller"""
    choice = ''

    for item in enumerate(installers):
        print "[%d] %s" % item

    try:
        idx = int(raw_input("Pick installer to use: "))
    except ValueError:
        print "Not a valid selection - unable to proceed."
        sys.exit(1)
    try:
        choice = installers[idx]
    except IndexError:
        print "Not a valid selection - unable to proceed."
        sys.exit(1)

    return choice

def createNBI(plist):
    """docstring for createNBI"""
    cmd = '/System/Library/CoreServices/System\ Image\ Utility.app/Contents/MacOS/imagetool'
    options = ' --plist ' + plist + ' > /dev/null 2>&1'
    fullcmd = cmd + options
    # print fullcmd
    subprocess.call(fullcmd, shell=True)

def convertNBI(dmgpath, mode = 'rw'):
    cmd = ['/usr/bin/hdiutil', 'attach', dmgpath,
                '-mountRandom', TMPDIR, '-nobrowse', '-plist',
                '-owners', 'on']
    proc = subprocess.Popen(cmd, bufsize=-1,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (pliststr, err) = proc.communicate()
    if proc.returncode:
        print >> sys.stderr, 'Error: "%s" while mounting %s.' % (err, dmgname)
    if pliststr:

def modifyNBI(items = None):
    """docstring for modifyNBI"""
    pass

TMPDIR = None
def main():
    """docstring for main"""
    if os.getuid() > 0:
        print 'This tool requires sudo or root access.'
        sys.exit(1)

    global TMPDIR

    usage = '---- Add usage text ----'

    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--source', '-s',
        help='Required. Path to Install Mac OS X Lion.app '
        'or Install OS X Mountain Lion.app or Install OS X Mavericks.app')
    parser.add_option('--destination', '-d',
        help='Required. Path to save .plist and .nbi files')
    parser.add_option('--name', '-n',
        help='Required. Name of the NBI, also applies to .plist')
    parser.add_option('--auto', '-a', action='store_true', default=False,
        help='Optional. Toggles automation mode, suitable for scripted runs')
    options, arguments = parser.parse_args()

    root = options.source
    destination = options.destination
    name = options.name
    auto = options.auto

    TMPDIR = tempfile.mkdtemp(dir=TMPDIR)

    # userSrc = '/Applications'
    # userDst = '/Users/bruienne/source/buildNBI/build'
    # userName = 'TESTING'
    
    if not destination.startswith('/'):
        destination = os.path.abspath(destination)
    
    print 'Locating installer...'
    source = locateInstaller(root, auto)
    
    print 'Generating plist...'
    if type(source) == list:
        choice = pickInstaller(source)
        plistfile = buildPlist(choice, destination, name)
    else:
        plistfile = buildPlist(source, destination, name)

    # print plistfile
    
    print 'Creating NBI...'
    createNBI(plistfile)

if __name__ == '__main__':
    main()
