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
#            Michael Lynn for the ServerInformation framework hackery
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
# * [--add-python][-p] Add the Python framework and libraries to the NBI
#   in order to support Python-based applications at runtime
#
# * [--add-ruby][-r] Add the Ruby framework and libraries to the NBI
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
from distutils.spawn import find_executable
from ctypes import CDLL, Structure, c_void_p, c_size_t, c_uint, c_uint32, c_uint64, create_string_buffer, addressof, sizeof, byref
import objc

sys.path.append("/usr/local/munki/munkilib")
import FoundationPlist
from xml.parsers.expat import ExpatError

def _get_mac_ver():
    import subprocess
    p = subprocess.Popen(['sw_vers', '-productVersion'], stdout=subprocess.PIPE)
    stdout, stderr = p.communicate()
    return stdout.strip()

# Setup access to the ServerInformation private framework to match board IDs to
#   model IDs if encountered (10.11 only so far) Code by Michael Lynn. Thanks!
class attrdict(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__

ServerInformation = attrdict()
ServerInformation_bundle = objc.loadBundle('ServerInformation',
                                            ServerInformation,
    bundle_path='/System/Library/PrivateFrameworks/ServerInformation.framework')

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
        retcode = subprocess.call(['/usr/bin/hdiutil', 'detach', '-force',
                                    mountpoint])
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


def buildplist(nbiindex, nbitype, nbidescription, nbiosversion, nbiname, nbienabled, isdefault, destdir=__file__):
    """buildplist takes a source, destination and name parameter that are used
        to create a valid plist for imagetool ingestion."""

    # Read and parse PlatformSupport.plist which has a reasonably reliable list
    #   of model IDs and board IDs supported by the OS X version being built

    nbipath = os.path.join(destdir, nbiname + '.nbi')
    platformsupport = FoundationPlist.readPlist(os.path.join(nbipath, 'i386', 'PlatformSupport.plist'))

    # OS X versions prior to 10.11 list both SupportedModelProperties and
    #   SupportedBoardIds - 10.11 only lists SupportedBoardIds. So we need to
    #   check both and append to the list if missing. Basically appends any
    #   model IDs found by looking up their board IDs to 'disabledsystemidentifiers'

    disabledsystemidentifiers = platformsupport.get('SupportedModelProperties') or []
    for boardid in platformsupport.get('SupportedBoardIds') or []:
        # Call modelPropertiesForBoardIDs from the ServerInfo framework to
        #   look up the model ID for this board ID.
        for sysid in ServerInformation.ServerInformationComputerModelInfo.modelPropertiesForBoardIDs_([boardid]):
            # If the returned model ID is not yet in 'disabledsystemidentifiers'
            #   add it, but not if it's an unresolved 'Mac-*' board ID.
            if sysid not in disabledsystemidentifiers and 'Mac-' not in sysid:
                disabledsystemidentifiers.append(sysid)

    nbimageinfo = {'IsInstall': True,
                   'Index': nbiindex,
                   'Kind': 1,
                   'Description': nbidescription,
                   'Language': 'Default',
                   'IsEnabled': nbienabled,
                   'SupportsDiskless': False,
                   'RootPath': 'NetInstall.dmg',
                   'EnabledSystemIdentifiers': sysidenabled,
                   'BootFile': 'booter',
                   'Architectures': ['i386'],
                   'BackwardCompatible': False,
                   'DisabledSystemIdentifiers': disabledsystemidentifiers,
                   'Type': nbitype,
                   'IsDefault': isdefault,
                   'Name': nbiname,
                   'osVersion': nbiosversion}

    plistfile = os.path.join(nbipath, 'NBImageInfo.plist')
    FoundationPlist.writePlist(nbimageinfo, plistfile)


def locateinstaller(rootpath='/Applications', auto=False):
    """locateinstaller will process the provided root path and looks for
        potential OS X installer apps containing InstallESD.dmg. Runs
        in interactive mode by default unless '-a' was provided at run"""

    # Remove a potential trailing slash (ie. from autocompletion)
    if rootpath.endswith('/'):
        rootpath = rootpath.rstrip('/')

    # The given path doesn't exist, bail
    if not os.path.exists(rootpath):
        print "The root path '" + rootpath + "' is not a valid path - unable " \
                                             "to proceed."
        sys.exit(1)

    # Auto mode specified but the root path is not the installer app, bail
    if auto and rootpath.endswith('com.apple.recovery.boot'):
        print 'Source is a Recovery partition, not mounting an InstallESD...'
        return rootpath
    elif auto and not rootpath.endswith('.app'):
        print 'Mode is auto but the rootpath is not an installer app or DMG, ' \
              ' unable to proceed.'
        sys.exit(1)

    # We're auto and the root path is an app - check InstallESD.dmg is there
    #   and return its location.
    elif rootpath.endswith('.app'):
        # Now look for the DMG
        if os.path.exists(os.path.join(rootpath, 'Contents/SharedSupport/InstallESD.dmg')):
            installsource = os.path.join(rootpath, 'Contents/SharedSupport/InstallESD.dmg')
            print("Install source is %s" % installsource)
            return installsource
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


def createnbi(workdir, description, osversion, name, enabled, nbiindex, nbitype, isdefault, dmgmount, root=None):
    """createnbi calls the 'createNetInstall.sh' script with the
        environment variables from the createvariables dict."""

    # Setup the path to our executable and pass it the CLI arguments
    # it expects to get: build root and DMG size. We use 7 GB to be safe.
    buildexec = os.path.join(BUILDEXECPATH, 'createNetInstall.sh')
    cmd = [buildexec, workdir, '7000']

    if root:
        if os.path.exists(os.path.join(root, 'Contents/SharedSupport/BaseSystem.dmg')):
            print("This is a 10.13 or newer installer, sourcing BaseSystem.dmg from SharedSupport.")
            dmgmount = root

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

    buildplist(nbiindex, nbitype, description, osversion, name, enabled, isdefault, workdir)

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

    if isHighSierra:
        enterprisedict = {}
        enterprisedict['SIU-SIP-setting'] = True
        enterprisedict['SIU-SKEL-setting'] = False
        enterprisedict['SIU-teamIDs-to-add'] = []

        plistlib.writePlist(enterprisedict, os.path.join(workdir, '.SIUSettings'))

# Example usage of the function:
# decompress('PayloadJava.cpio.xz', 'PayloadJava.cpio')
# Decompresses a xz compressed file from the first input file path to the second output file path

class lzma_stream(Structure):
    _fields_ = [
        ("next_in",        c_void_p),
        ("avail_in",       c_size_t),
        ("total_in",       c_uint64),
        ("next_out",       c_void_p),
        ("avail_out",      c_size_t),
        ("total_out",      c_uint64),
        ("allocator",      c_void_p),
        ("internal",       c_void_p),
        ("reserved_ptr1",  c_void_p),
        ("reserved_ptr2",  c_void_p),
        ("reserved_ptr3",  c_void_p),
        ("reserved_ptr4",  c_void_p),
        ("reserved_int1",  c_uint64),
        ("reserved_int2",  c_uint64),
        ("reserved_int3",  c_size_t),
        ("reserved_int4",  c_size_t),
        ("reserved_enum1", c_uint),
        ("reserved_enum2", c_uint),
    ]

# Hardcoded this path to the System liblzma dylib location, so that /usr/local/lib or other user
# installed library locations aren't used (which ctypes.util.find_library(...) would hit).
# Available in OS X 10.7+
c_liblzma = CDLL('/usr/lib/liblzma.dylib')

NULL               = None
BUFSIZ             = 65535
LZMA_OK            = 0
LZMA_RUN           = 0
LZMA_FINISH        = 3
LZMA_STREAM_END    = 1
BLANK_BUF          = '\x00'*BUFSIZ
UINT64_MAX         = c_uint64(18446744073709551615)
LZMA_CONCATENATED  = c_uint32(0x08)
LZMA_RESERVED_ENUM = 0
LZMA_STREAM_INIT   = [NULL, 0, 0, NULL, 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, 0, 0, 0, 0, LZMA_RESERVED_ENUM, LZMA_RESERVED_ENUM]

def decompress(infile, outfile):
    # Create an empty lzma_stream object
    strm = lzma_stream(*LZMA_STREAM_INIT)

    # Initialize a decoder
    result = c_liblzma.lzma_stream_decoder(byref(strm), UINT64_MAX, LZMA_CONCATENATED)

    # Setup the output buffer
    outbuf = create_string_buffer(BUFSIZ)
    strm.next_out  = addressof(outbuf)
    strm.avail_out = sizeof(outbuf)

    # Setup the (blank) input buffer
    inbuf  = create_string_buffer(BUFSIZ)
    strm.next_in = addressof(inbuf)
    strm.avail_in = 0

    # Read in the input .xz file
    # ... Not the best way to do things because it reads in the entire file - probably not great for GB+ size

    # f_in = open(infile, 'rb')
    # xz_file = f_in.read()
    # f_in.close()
    xz_file = open(infile, 'rb')

    cursor = 0
    xz_file.seek(0,2)
    EOF = xz_file.tell()
    xz_file.seek(0)

    # Open up our output file
    f_out = open(outfile, 'wb')

    # Start with a RUN action
    action = LZMA_RUN
    # Keep looping while we're processing
    while True:
        # Check if decoder has consumed the current input buffer and we have remaining data
        if ((strm.avail_in == 0) and (cursor < EOF)):
            # Load more data!
            # In theory, I shouldn't have to clear the input buffer, but I'm paranoid
            # inbuf[:] = BLANK_BUF
            # Now we load it:
            # - Attempt to take a BUFSIZ chunk of data
            input_chunk = xz_file.read(BUFSIZ)
            # - Measure how much we actually got
            input_len   = len(input_chunk)
            # - Assign the data to the buffer
            inbuf[0:input_len] = input_chunk
            # - Configure our chunk input information
            strm.next_in  = addressof(inbuf)
            strm.avail_in = input_len
            # - Adjust our cursor
            cursor += input_len
            # - If the cursor is at the end, switch to FINISH action
            if (cursor >= EOF):
                action = LZMA_FINISH
        # If we're here, we haven't completed/failed, so process more data!
        result = c_liblzma.lzma_code(byref(strm), action)
        # Check if we filled up the output buffer / completed running
        if ((strm.avail_out == 0) or (result == LZMA_STREAM_END)):
            # Write out what data we have!
            # - Measure how much we got
            output_len   = BUFSIZ - strm.avail_out
            # - Get that much from the buffer
            output_chunk = outbuf.raw[:output_len]
            # - Write it out
            f_out.write(output_chunk)
            # - Reset output information to a full available buffer
            # (Intentionally not clearing the output buffer here .. but probably could?)
            strm.next_out  = addressof(outbuf)
            strm.avail_out = sizeof(outbuf)
        if (result != LZMA_OK):
            if (result == LZMA_STREAM_END):
                # Yay, we finished
                result = c_liblzma.lzma_end(byref(strm))
                return True
            # If we got here, we have a problem
            # Error codes are defined in xz/src/liblzma/api/lzma/base.h (LZMA_MEM_ERROR, etc.)
            # Implementation of pretty English error messages is an exercise left to the reader ;)
            raise Exception("Error: return code of value %s - naive decoder couldn't handle input!" % (result))


class processNBI(object):
    """The processNBI class provides the makerw(), modify() and close()
        functions. All functions serve to make modifications to an NBI
        created by createnbi()"""

    # Don't think we need this.
    def __init__(self, customfolder = None, enablepython=False, enableruby=False, utilplist=False):
         super(processNBI, self).__init__()
         self.customfolder = customfolder
         self.enablepython = enablepython
         self.enableruby = enableruby
         self.utilplist = utilplist
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
        return [ self.hdiutil, 'detach', '-force',
                          detach_mountpoint ]
    def dmgconvert(self, convert_source, convert_target, shadow_file, mode):
        # We have a shadow file, so use it. Otherwise don't.
        if shadow_file:
            command = [ self.hdiutil, 'convert',
                              '-format', mode,
                              '-o', convert_target,
                              '-shadow', shadow_file,
                              convert_source ]
        else:
            command = [ self.hdiutil, 'convert',
                              '-format', mode,
                              '-o', convert_target,
                              convert_source ]
        return command

    def dmgresize(self, resize_source, shadow_file=None, size=None):

        print "Will resize DMG at mount: %s" % resize_source

        if shadow_file:
            return [ self.hdiutil, 'resize',
                          '-size', size,
                          '-shadow', shadow_file,
                          resize_source ]
        else:
            proc = subprocess.Popen(['/usr/bin/hdiutil', 'resize', '-limits', resize_source],
                                      bufsize=-1, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)

            (output, err) = proc.communicate()

            size = output.split('\t')[0]

            return [ self.hdiutil, 'resize',
                          '-size', '%sb' % size, resize_source ]

    def xarextract(self, xar_source, sysplatform):

        if 'darwin' in sysplatform:
            return [ '/usr/bin/xar', '-x',
                                     '-f', xar_source,
                                     'Payload',
                                     '-C', TMPDIR ]
        else:
            # TO-DO: decompress xz lzma with Python
            pass
    def cpioextract(self, cpio_archive, pattern):
        return [ '/usr/bin/cpio -idmu --quiet -I %s %s' % (cpio_archive, pattern) ]
    def xzextract(self, xzexec, xzfile):
        return ['/usr/local/bin/xz -d %s' % xzfile]
    def getfiletype(self, filepath):
        return ['/usr/bin/file', filepath]

    def runcmd(self, cmd, cwd=None):

        # print cmd

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

    # Code for parse_pbzx from https://gist.github.com/pudquick/ff412bcb29c9c1fa4b8d
    # Further write-up: https://gist.github.com/pudquick/29fcfe09c326a9b96cf5
    def seekread(self, f, offset=None, length=0, relative=True):
        if (offset != None):
            # offset provided, let's seek
            f.seek(offset, [0,1,2][relative])
        if (length != 0):
            return f.read(length)

    def parse_pbzx(self, pbzx_path):
        import struct

        archivechunks = []
        section = 0
        xar_out_path = '%s.part%02d.cpio.xz' % (pbzx_path, section)
        f = open(pbzx_path, 'rb')
        # pbzx = f.read()
        # f.close()
        magic = self.seekread(f,length=4)
        if magic != 'pbzx':
            raise "Error: Not a pbzx file"
        # Read 8 bytes for initial flags
        flags = self.seekread(f,length=8)
        # Interpret the flags as a 64-bit big-endian unsigned int
        flags = struct.unpack('>Q', flags)[0]
        xar_f = open(xar_out_path, 'wb')
        archivechunks.append(xar_out_path)
        while (flags & (1 << 24)):
            # Read in more flags
            flags = self.seekread(f,length=8)
            flags = struct.unpack('>Q', flags)[0]
            # Read in length
            f_length = self.seekread(f,length=8)
            f_length = struct.unpack('>Q', f_length)[0]
            xzmagic = self.seekread(f,length=6)
            if xzmagic != '\xfd7zXZ\x00':
                # This isn't xz content, this is actually _raw decompressed cpio_ chunk of 16MB in size...
                # Let's back up ...
                self.seekread(f,offset=-6,length=0)
                # ... and split it out ...
                f_content = self.seekread(f,length=f_length)
                section += 1
                decomp_out = '%s.part%02d.cpio' % (pbzx_path, section)
                g = open(decomp_out, 'wb')
                g.write(f_content)
                g.close()
                archivechunks.append(decomp_out)
                # Now to start the next section, which should hopefully be .xz (we'll just assume it is ...)
                xar_f.close()
                section += 1
                new_out = '%s.part%02d.cpio.xz' % (pbzx_path, section)
                xar_f = open(new_out, 'wb')
                archivechunks.append(new_out)
            else:
                f_length -= 6
                # This part needs buffering
                f_content = self.seekread(f,length=f_length)
                tail = self.seekread(f,offset=-2,length=2)
                xar_f.write(xzmagic)
                xar_f.write(f_content)
                if tail != 'YZ':
                    xar_f.close()
                    raise "Error: Footer is not xar file footer"

        try:
            f.close()
            xar_f.close()
        except:
            pass

        return archivechunks

    def processframeworkpayload(self, payloadsource, payloadtype, cpio_archive):
        # Check filetype of the Payload, 10.10 adds a pbzx wrapper
        if payloadtype.startswith('data'):
            # This is most likely pbzx-wrapped, unwrap it
            print("Payload %s is PBZX-wrapped, unwrapping..." % payloadsource)
            chunks = self.parse_pbzx(payloadsource)
            os.remove(payloadsource)
            fout = file(os.path.join(TMPDIR, cpio_archive), 'wb')

            for xzfile in chunks:
                if '.xz' in xzfile and os.path.getsize(xzfile) > 0:
                    print('Decompressing %s' % xzfile)

                    xzexec = find_executable('xz')

                    if xzexec is not None:
                        print("Found xz executable at %s..." % xzexec)
                        result = self.runcmd(self.xzextract(xzexec, xzfile), cwd=TMPDIR)
                    else:
                        print("No xz executable found, using decompress()")
                        result = decompress(xzfile, xzfile.strip('.xz'))
                        os.remove(xzfile)

                    fin = file(xzfile.strip('.xz'), 'rb')
                    print("-------------------------------------------------------------------------")
                    print("Concatenating %s" % cpio_archive)
                    shutil.copyfileobj(fin, fout, 65536)
                    fin.close()
                    os.remove(fin.name)
                else:
                    fin = file(xzfile, 'rb')
                    print("-------------------------------------------------------------------------")
                    print("Concatenating %s" % cpio_archive)
                    shutil.copyfileobj(fin, fout, 65536)
                    fin.close()
                    os.remove(fin.name)

            fout.close()

        else:
            # No pbzx wrapper, rename and move to cpio extraction
            os.rename(payloadsource, cpio_archive)


    # Allows modifications to be made to a DMG previously made writable by
    #   processNBI.makerw()
    def modify(self, nbimount, dmgpath, nbishadow, installersource):

        addframeworks = []
        if self.enablepython:
            addframeworks.append('python')
        if self.enableruby:
            addframeworks.append('ruby')

        # Define the needed source PKGs for our frameworks
        if isSierra or isHighSierra:
            # In Sierra pretty much everything is in Essentials.
            # We also need to add libssl as it's no longer standard.
            payloads = { 'python': {'sourcepayloads': ['Essentials'],
                                    'regex': '\"*Py*\" \"*py*\" \"*libssl*\" \"*libffi.dylib*\" \"*libexpat*\"'},
                         'ruby': {'sourcepayloads': ['Essentials'],
                                  'regex': '\"*ruby*\" \"*lib*ruby*\" \"*Ruby.framework*\"  \"*libssl*\"'}
                       }
        elif isElCap:
            # In ElCap pretty much everything is in Essentials.
            # We also need to add libssl as it's no longer standard.
            payloads = { 'python': {'sourcepayloads': ['Essentials'],
                                    'regex': '\"*Py*\" \"*py*\" \"*libssl*\"'},
                         'ruby': {'sourcepayloads': ['Essentials'],
                                  'regex': '\"*ruby*\" \"*lib*ruby*\" \"*Ruby.framework*\"  \"*libssl*\"'}
                       }
        else:
            payloads = { 'python': {'sourcepayloads': ['BSD'],
                                    'regex': '\"*Py*\" \"*py*\"'},
                         'ruby': {'sourcepayloads': ['BSD', 'Essentials'],
                                  'regex': '\"*ruby*\" \"*lib*ruby*\" \"*Ruby.framework*\"'}
                       }
        # Set 'modifybasesystem' if any frameworks are to be added, we're building
        #   an ElCap NBI or if we're adding a custom Utilites plist
        modifybasesystem = (len(addframeworks) > 0 or isElCap or isSierra or isHighSierra or self.utilplist)

        # If we need to make modifications to BaseSystem.dmg we mount it r/w
        if modifybasesystem:
            # Setup the BaseSystem.dmg for modification by mounting it with a shadow
            # and resizing the shadowed image, 10 GB should be good. We'll shrink
            # it again later.
            if not isHighSierra:
                basesystemshadow = os.path.join(TMPDIR, 'BaseSystem.shadow')
                basesystemdmg = os.path.join(nbimount, 'BaseSystem.dmg')
            else:
                print("Install source is 10.13 or newer, BaseSystem.dmg is in an alternate location...")
                basesystemshadow = os.path.join(TMPDIR, 'BaseSystem.shadow')
                basesystemdmg = os.path.join(nbimount, 'Install macOS High Sierra Beta.app/Contents/SharedSupport/BaseSystem.dmg')

            print("Running self.dmgresize...")
            result = self.runcmd(self.dmgresize(basesystemdmg, basesystemshadow, '8G'))
            print("Running self.dmgattach...")
            plist = self.runcmd(self.dmgattach(basesystemdmg, basesystemshadow))

            # print("Contents of plist:\n------\n%s\n------" % plist)

            basesystemplist = plistlib.readPlistFromString(plist)
            
            # print("Contents of basesystemplist:\n------\n%s\n------" % basesystemplist)

            for entity in basesystemplist['system-entities']:
                if 'mount-point' in entity:
                    basesystemmountpoint = entity['mount-point']

        # OS X 10.11 El Capitan triggers an Installer Progress app which causes
        #   custom installer workflows using 'Packages/Extras' to fail so
        #   we need to nix it. Thanks, Apple.
        if isSierra or isHighSierra:
            rcdotinstallpath = os.path.join(basesystemmountpoint, 'private/etc/rc.install')
            rcdotinstallro = open(rcdotinstallpath, "r")
            rcdotinstalllines = rcdotinstallro.readlines()
            rcdotinstallro.close()
            rcdotinstallw = open(rcdotinstallpath, "w")

            # The binary changed to launchprogresswindow for Sierra, still killing it.
            # Sierra also really wants to launch the Language Chooser which kicks off various install methods.
            # This can mess with some third party imaging tools (Imagr) so we simply change it to 'echo'
            #   so it simply echoes the args Language Chooser would be called with instead of launching LC, and nothing else.
            for line in rcdotinstalllines:
                # Remove launchprogresswindow
                if line.rstrip() != "/System/Installation/CDIS/launchprogresswindow &":
                    # Rewrite $LAUNCH as /bin/echo
                    if line.rstrip() == "LAUNCH=\"/System/Library/CoreServices/Language Chooser.app/Contents/MacOS/Language Chooser\"":
                        rcdotinstallw.write("LAUNCH=/bin/echo")
                        # Add back ElCap code to source system imaging extras files
                        rcdotinstallw.write("\nif [ -x /System/Installation/Packages/Extras/rc.imaging ]; then\n\t/System/Installation/Packages/Extras/rc.imaging\nfi")
                    else:
                        rcdotinstallw.write(line)

            rcdotinstallw.close()

        if isElCap:
            rcdotinstallpath = os.path.join(basesystemmountpoint, 'private/etc/rc.install')
            rcdotinstallro = open(rcdotinstallpath, "r")
            rcdotinstalllines = rcdotinstallro.readlines()
            rcdotinstallro.close()
            rcdotinstallw = open(rcdotinstallpath, "w")
            for line in rcdotinstalllines:
                if line.rstrip() != "/System/Library/CoreServices/Installer\ Progress.app/Contents/MacOS/Installer\ Progress &":
                    rcdotinstallw.write(line)
            rcdotinstallw.close()

        if isElCap or isSierra or isHighSierra:
            # Reports of slow NetBoot speeds with 10.11+ have lead others to
            #   remove various launch items that seem to cause this. Remove some
            #   of those as a stab at speeding things back up.
            baseldpath = os.path.join(basesystemmountpoint, 'System/Library/LaunchDaemons')
            launchdaemonstoremove = ['com.apple.locationd.plist',
                                     'com.apple.lsd.plist',
                                     'com.apple.tccd.system.plist',
                                     'com.apple.ocspd.plist']

            for ld in launchdaemonstoremove:
                ldfullpath = os.path.join(baseldpath, ld)
                if os.path.exists(ldfullpath):
                    os.unlink(ldfullpath)
        # Handle any custom content to be added, customfolder has a value
        if self.customfolder:
            print("-------------------------------------------------------------------------")
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

            # Copy over the custom folder contents. If the folder didn't exists
            # we can skip the above removal and get straight to copying.
            os.mkdir(processdir)
            print('Copying ' + self.customfolder + ' to ' + processdir + '...')
            distutils.dir_util.copy_tree(self.customfolder, processdir)

        # Is Python or Ruby being added? If so, do the work.
        if addframeworks:

            # Create an empty list to record cached Payload resources
            havepayload = []

            # Loop through the frameworks we've been asked to include
            for framework in addframeworks:

                # Get the cpio glob pattern/regex to extract the framework
                regex = payloads[framework]['regex']
                print("-------------------------------------------------------------------------")
                print("Adding %s framework from %s to NBI at %s" % (framework.capitalize(), installersource, nbimount))

                # Loop through all possible source payloads for this framework
                for payload in payloads[framework]['sourcepayloads']:

                    payloadsource = os.path.join(TMPDIR, 'Payload')
                    # os.rename(payloadsource, payloadsource + '-' + payload)
                    # payloadsource = payloadsource + '-' + payload
                    cpio_archive = payloadsource + '-' + payload + '.cpio.xz'
                    xar_source = os.path.join(installersource, 'Packages', payload + '.pkg')

                    print("Cached payloads: %s" % havepayload)

                    # Check whether we already have this Payload from a previous run
                    if cpio_archive not in havepayload:

                        print("-------------------------------------------------------------------------")
                        print("No cache, extracting %s" % xar_source)

                        # Extract Payload(s) from desired OS X installer package
                        sysplatform = sys.platform
                        self.runcmd(self.xarextract(xar_source, sysplatform))

                        # Determine the Payload file type using 'file'
                        payloadtype = self.runcmd(self.getfiletype(payloadsource)).split(': ')[1]

                        print("Processing payloadsource %s" % payloadsource)
                        result = self.processframeworkpayload(payloadsource, payloadtype, cpio_archive)

                        # Log that we have this cpio_archive in case we need it later
                        if cpio_archive not in havepayload:
                            # print("Adding cpio_archive %s to havepayload" % cpio_archive)
                            havepayload.append(cpio_archive)

                    # Extract our needed framework bits from CPIO arch
                    #   using shell globbing pattern(s)
                    print("-------------------------------------------------------------------------")
                    print("Processing cpio_archive %s" % cpio_archive)
                    self.runcmd(self.cpioextract(cpio_archive, regex),
                                cwd=basesystemmountpoint)

            for cpio_archive in havepayload:
                print("-------------------------------------------------------------------------")
                print("Removing cached Payload %s" % cpio_archive)
                if os.path.exists(cpio_archive):
                    os.remove(cpio_archive)

        # Add custom Utilities.plist if passed as an argument
        if self.utilplist:
            print("-------------------------------------------------------------------------")
            print("Adding custom Utilities.plist from %s" % self.utilplist)
            try:
                shutil.copyfile(os.path.abspath(self.utilplist), os.path.join(basesystemmountpoint,
                                'System/Installation/CDIS/OS X Utilities.app/Contents/Resources/Utilities.plist'))
            except:
                print("Failed to add custom Utilites plist from %s" % self.utilplist)

        if modifybasesystem and basesystemmountpoint:

            # Done adding frameworks to BaseSystem, unmount and convert
            # detachresult = self.runcmd(self.dmgdetach(basesystemmountpoint))
            detachresult = unmountdmg(basesystemmountpoint)

            # Set some DMG conversion targets for later
            basesystemrw = os.path.join(TMPDIR, 'BaseSystemRW.dmg')
            basesystemro = os.path.join(TMPDIR, 'BaseSystemRO.dmg')

            # Convert to UDRW, the only format that will allow resizing the BaseSystem.dmg later
            convertresult = self.runcmd(self.dmgconvert(basesystemdmg, basesystemrw, basesystemshadow, 'UDRW'))
            # Delete the original DMG, we need to clear up some space where possible
            os.remove(basesystemdmg)

            # Resize BaseSystem.dmg to its smallest possible size (using hdiutil resize -limits)
            resizeresult = self.runcmd(self.dmgresize(basesystemrw))

            # Convert again, to UDRO, to shrink the final DMG size more
            convertresult = self.runcmd(self.dmgconvert(basesystemrw, basesystemro, None, 'UDRO'))

            # Rename the finalized DMG to its intended name BaseSystem.dmg
            shutil.copyfile(basesystemro, basesystemdmg)

        # We're done, unmount the outer NBI DMG.
        unmountdmg(nbimount)

        # Convert modified DMG to .sparseimage, this will shrink the image
        # automatically after modification.
        print("-------------------------------------------------------------------------")
        print "Sealing DMG at path %s" % (dmgpath)
        dmgfinal = convertdmg(dmgpath, nbishadow)
        # print('Got back final DMG as ' + dmgfinal + ' from convertdmg()...')

        # Do some cleanup, remove original DMG, its shadow file and rename
        # .sparseimage to NetInstall.dmg
        os.remove(nbishadow)
        os.remove(dmgpath)
        os.rename(dmgfinal, dmgpath)


TMPDIR = None
sysidenabled = []
isElCap = False
isSierra = False
isHighSierra = False

if LooseVersion(_get_mac_ver()) >= "10.13":
    BUILDEXECPATH = ('/System/Library/PrivateFrameworks/SIUFoundation.framework/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')
    isHighSierra = True
elif LooseVersion(_get_mac_ver()) >= "10.12":
    BUILDEXECPATH = ('/System/Library/PrivateFrameworks/SIUFoundation.framework/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')
    isSierra = True
elif LooseVersion(_get_mac_ver()) >= "10.11":
    BUILDEXECPATH = ('/System/Library/PrivateFrameworks/SIUFoundation.framework/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')
    isElCap = True
elif LooseVersion(_get_mac_ver()) < "10.10":
    BUILDEXECPATH = ('/System/Library/CoreServices/System Image Utility.app/Contents/Frameworks/SIUFoundation.framework/'
                 'Versions/A/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')
else:
    BUILDEXECPATH = ('/System/Library/CoreServices/Applications/System Image Utility.app/Contents/Frameworks/SIUFoundation.framework/'
                 'Versions/A/XPCServices/com.apple.SIUAgent.xpc/Contents/Resources')

def main():
    """Main routine"""

    global TMPDIR
    global sysidenabled

    # TBD - Full usage text
    usage = ('Usage: %prog --source/-s <path>\n'
             '                   --name/-n MyNBI\n'
             '                   [--destination/-d] <path>\n'
             '                   [--folder/-f] FolderName\n'
             '                   [--auto/-a]\n'
             '                   [--enable/-e]\n'
             '                   [--default]\n'
             '                   [--index]\n'
             '                   [--sysid-enable]\n'
             '                   [--type]\n'
             '                   [--add-python/-p]\n'
             '                   [--add-ruby/-r]\n'
             '                   [--utilities-plist]\n\n'
             '    %prog creates an OS X 10.7, 10.8, 10.9, 10.10, 10.11 or 10.12\n'
             '    NetInstall NBI ready for use with a NetBoot server.\n\n'
             '    The NBI target OS X version must match that of the host OS.\n\n'
             '    An option to modify the NBI\'s NetInstall.dmg is also provided\n'
             '    by specifying an optional name of a folder in the source root\n'
             '    to add or replace on the NetInstall.dmg.\n\n'
             '    Examples:\n'
             '    Run interactively, pick OS X installer from /Applications:\n'
             '    $ ./AutoNBI.py -s /Applications -d ~/BuildRoot -n MyNBI\n\n'
             '    Run non-interactively, use Mavericks installer app as source:\n'
             '    $ ./AutoNBI.py -s /Volumes/Disk/Install OS X Mavericks.app -d'
             ' ~/BuildRoot -n MyNBI -a\n\n'
             '    Run non-interactively, use an InstallESD.dmg file as source\n'
             '    and replace the Packages folder on the resulting NBI:\n'
             '    $ ./AutoNBI.py -s ~/Documents/InstallESD.dmg -d ~/BuildRoot -n MyNBI'
             ' -f Packages -a\n\n'
             '    Run non-interactively, use the Yosemite installer as source,\n'
             '    replace Packages folder, enable the NBI, use index 6667 and\n'
             '    enable support for MacBookPro12,1 models only:\n'
             '    $ ./AutoNBI.py --source /Applications/Install\ OS\ X\ Yosemite.app\n'
             '                   --destination /tmp \\\n'
             '                   --name Imagr \\\n'
             '                   --folder Packages \\\n'
             '                   --auto --default --enable \\\n'
             '                   --index 6667 \\\n'
             '                   --sysid-enable MacBookPro12,1')

    # Setup a parser instance
    parser = optparse.OptionParser(usage=usage)

    # Setup the recognized options
    parser.add_option('--source', '-s',
                      help='Required. Path to Install Mac OS X Lion.app '
                           'or Install OS X Mountain Lion.app or Install OS X Mavericks.app')
    parser.add_option('--name', '-n',
                      help='Required. Name of the NBI, also applies to .plist')
    parser.add_option('--destination', '-d', default=os.getcwd(),
                      help='Optional. Path to save .plist and .nbi files. Defaults to CWD.')
    parser.add_option('--folder', '-f', default='',
                      help='Optional. Name of a folder on the NBI to modify. This will be the\
                            root below which changes will be made')
    parser.add_option('--auto', '-a', action='store_true', default=False,
                      help='Optional. Toggles automation mode, suitable for scripted runs')
    parser.add_option('--enable-nbi', '-e', action='store_true', default=False,
                      help='Optional. Marks NBI as enabled (IsEnabled = True).', dest='enablenbi')
    parser.add_option('--add-ruby', '-r', action='store_true', default=False,
                      help='Optional. Enables Ruby in BaseSystem.', dest='addruby')
    parser.add_option('--add-python', '-p', action='store_true', default=False,
                      help='Optional. Enables Python in BaseSystem.', dest='addpython')
    parser.add_option('--utilities-plist', action='store_true', default=False,
                      help='Optional. Add a custom Utilities.plist to modify the menu.', dest='utilplist')
    parser.add_option('--default', action='store_true', default=False,
                      help='Optional. Marks the NBI as the default for all clients. Only one default should be '
                           'enabled on any given NetBoot/NetInstall server.', dest='isdefault')
    parser.add_option('--index', default=5000, dest='nbiindex', type='int',
                      help='Optional. Set a custom Index for the NBI. Default is 5000.')
    parser.add_option('--type', default='NFS', dest='nbitype',
                      help='Optional. Set a custom Type for the NBI. HTTP or NFS. Default is NFS.')
    parser.add_option('--sysid-enable', dest='sysidenabled', action='append', type='str',
                      help='Optional. Whitelist a given System ID (\'MacBookPro10,1\') Can be '
                           'defined multiple times. WARNING: This will enable ONLY the listed '
                           'System IDs. Systems not explicitly marked as enabled will not be '
                           'able to boot from this NBI.')

    # Parse the provided options
    options, arguments = parser.parse_args()

    # Check our passed options, at least source, destination and name are required
    if options is None:
        parser.print_help()
        sys.exit(-1)

    if not options.source:
        parser.error('Missing --source flag, stopping.')
    if not options.name:
        parser.error('Missing --name flag, stopping.')

    # Get the root path now, we need to test and bail if it's not found soon.
    root = options.source

    # Are we root?
    if os.getuid() != 0:
        parser.print_usage()
        print >> sys.stderr, 'This tool requires sudo or root privileges.'
        exit(-1)

    if not os.path.exists(root):
        print >> sys.stderr, 'The given source at %s does not exist.' % root
        exit(-1)

    # Setup our base requirements for installer app root path, destination,
    #   name of the NBI and auto mode.
    destination = options.destination
    auto = options.auto
    enablenbi = options.enablenbi
    customfolder = options.folder
    addpython = options.addpython
    addruby = options.addruby
    name = options.name
    utilplist = options.utilplist
    isdefault = options.isdefault
    nbiindex = options.nbiindex
    nbitype = options.nbitype
    if options.sysidenabled:
        sysidenabled = options.sysidenabled
        print('Enabling System IDs: %s' % sysidenabled)

    # Set 'modifydmg' if any of 'addcustom', 'addpython' or 'addruby' are true
    addcustom = len(customfolder) > 0
    modifynbi = (addcustom or addpython or addruby or isElCap or isSierra or isHighSierra)

    # Spin up a tmp dir for mounting
    TMPDIR = tempfile.mkdtemp(dir=TMPDIR)

    # Now we start a typical run of the tool, first locate one or more
    #   installer app candidates

    if os.path.isdir(root):
        print 'Locating installer...'
        source = locateinstaller(root, auto)
        shouldcreatenbi = True
    elif mimetypes.guess_type(root)[0].endswith('diskimage'):
        print 'Source is a disk image.'
        if 'NetInstall' in root:
            print('Disk image is an existing NetInstall, will modify only...')
            shouldcreatenbi = False
        elif 'InstallESD' in root:
            print('Disk image is an InstallESD, will create new NetInstall...')
            shouldcreatenbi = True
        source = root

    else:
        print 'Source is neither an installer app or InstallESD.dmg.'
        sys.exit(-1)

    if shouldcreatenbi:
        # If the destination path isn't absolute, we make it so to prevent errors
        if not destination.startswith('/'):
            destination = os.path.abspath(destination)

        # If we have a list for our source, more than one installer app was found
        #   so we run the list through pickinstaller() to pick one interactively
        if type(source) == list:
            source = pickinstaller(source)

        # Prep the build root - create it if it's not there
        if not os.path.exists(destination):
            os.mkdir(destination)

        if source.endswith('dmg'):
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
        elif source.endswith('com.apple.recovery.boot'):
            mount = source
        else:
            print 'Install source is neither InstallESD nor Recovery drive, this is bad.'
            sys.exit(-1)

        if not isHighSierra:
            osversion, osbuild, unused = getosversioninfo(mount)
        else:
            osversion, osbuild, unused = getosversioninfo(os.path.join(root, 'Contents/SharedSupport'))

        if not isSierra or not isHighSierra:
            description = "OS X %s - %s" % (osversion, osbuild)
        else:
            description = "macOS %s - %s" % (osversion, osbuild)

        # Prep our build root for NBI creation
        print 'Prepping ' + destination + ' with source mounted at ' + mount
        prepworkdir(destination)

        # Now move on to the actual NBI creation
        print 'Creating NBI at ' + destination
        print 'Base NBI Operating System is ' + osversion
        createnbi(destination, description, osversion, name, enablenbi, nbiindex, nbitype, isdefault, mount, root)

    # Make our modifications if any were provided from the CLI
    if modifynbi:
        if addcustom:
            try:
                if os.path.isdir(customfolder):
                    customfolder = os.path.abspath(customfolder)
            except IOError:
                print("%s is not a valid path - unable to proceed." % customfolder)
                sys.exit(1)

        # Path to the NetInstall.dmg
        if shouldcreatenbi:
            netinstallpath = os.path.join(destination, name + '.nbi', 'NetInstall.dmg')
        else:
            netinstallpath = root
            mount = None

        # Initialize a new processNBI() instance as 'nbi'
        nbi = processNBI(customfolder, addpython, addruby, utilplist)

        # Run makerw() to enable modifications
        nbimount, nbishadow = nbi.makerw(netinstallpath)

        print("NBI mounted at %s" % nbimount)

        nbi.modify(nbimount, netinstallpath, nbishadow, mount)

        # We're done, unmount all the things
        if shouldcreatenbi:
            unmountdmg(mount)

        distutils.dir_util.remove_tree(TMPDIR)

        print("-------------------------------------------------------------------------")
        print 'Modifications complete...'
        print 'Done.'
    else:
        # We're done, unmount all the things
        unmountdmg(mount)
        distutils.dir_util.remove_tree(TMPDIR)

        print("-------------------------------------------------------------------------")
        print 'No modifications will be made...'
        print 'Done.'

if __name__ == '__main__':
    main()
