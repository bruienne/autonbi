#!/usr/bin/python

import os
import sys
sys.path.append("/usr/local/munki/munkilib")
import FoundationPlist
import subprocess

def buildPlist(source = '', dest = __file__, name = ''):
    """docstring for buildPlist"""

    # Set variables for the output plist, path to the source DMG inside the app bundle and the NBI's index
    if dest == __file__:
        destdir = os.path.dirname(dest)
    else:
        destdir = dest

    baselocation = os.path.join(destdir , name)
    plistfile = baselocation + '.plist'
    nbilocation = baselocation
    # print plistfile

    dmgpath = os.path.join(source, 'Contents/SharedSupport/InstallESD.dmg')
    index = 5000 # TBD: figure out a way to keep track of previous idxs
    
    # Initialize an empty dict that will hold the plist contents
    nbiconfig = {}
    
    nbiconfig['automatedInstall'] = {'eraseTarget': True, 'language': 'en', 'targetType': True, 'targetVolume': 'Macintosh HD'}
    nbiconfig['sourcesList'] = [{'dmgPath': dmgpath, 'isInstallMedia': True, 'kindOfSource': 1, 'sourceType': 'ESDApplication', 'volumePath': source}]
    nbiconfig['imageDescription'] = 'Auto-build of ' + name
    nbiconfig['imageIndex'] = index
    nbiconfig['imageName'] = name
    nbiconfig['installType'] = 'netinstall'
    nbiconfig['nbiLocation'] = nbilocation
    
    # Write out the now-complete dict as a standard plist to our previously configured destination file
    FoundationPlist.writePlist(nbiconfig, plistfile)
    
    # Return the path to the configuration plist to the caller
    return plistfile
    # return nbiconfig

def locateInstaller(rootpath = '/Applications', auto = False):
    """docstring for locateInstaller"""
    
    if not os.path.exists(rootpath):
        print "The root path " + rootpath + " is not a valid path - unable to proceed."
        sys.exit(1)
    elif auto and rootpath == '':
        print 'Mode is auto but no rootpath was given, unable to proceed.'
        sys.exit(1)
    elif auto and not rootpath.endswith('.app'):
        print 'Mode is auto but the rootpath is not an installer app, unable to proceed.'
        sys.exit(1)
    elif auto and rootpath.endswith('.app'):
        return rootpath
    else:
        # Initialize an empty list to store all found OS X installer apps
        installers = []
        for item in os.listdir(rootpath):
            if item.startswith('Install OS X'):
                for unused_dirs, unused_paths, files in os.walk(os.path.join(rootpath, item)):
                    for file in files:
                        if file.endswith('InstallESD.dmg'):
                            installers.append(os.path.join(rootpath, item))
        if len(installers) == 0:
            print 'No suitable installers found in ' + rootpath + ' - unable to proceed.'
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
    cmd = "/System/Library/CoreServices/System\ Image\ Utility.app/Contents/MacOS/imagetool"
    options = ' --plist ' + plist + ' > /dev/null 2>&1'
    fullcmd = cmd + options
    print fullcmd
    subprocess.call(fullcmd, shell=True)

def convertNBI(mode = 'rw'):
    pass

def modifyNBI(items = None):
    """docstring for modifyNBI"""
    pass

def main():
    """docstring for main"""

    source = locateInstaller('/Applications')
    
    if len(source) > 1:
        source = pickInstaller(source)
        plistfile = buildPlist(source, '/Users/bruienne/source/buildNBI/build', 'TESTING')
    else:
        plistfile = buildPlist(source, '/Users/bruienne/source/buildNBI/build', 'TESTING')
    
    # print plistfile
    
    createNBI(plistfile)

if __name__ == '__main__':
    main()
