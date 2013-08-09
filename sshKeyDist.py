import os
import subprocess
import ssh
import wx
import wx.lib.newevent
import re
from StringIO import StringIO
import logging
from threading import *
import threading
import time
import sys
from os.path import expanduser
import subprocess
import traceback
import socket
from utilityFunctions import HelpDialog
import pkgutil
import signal

from logger.Logger import logger
from PassphraseDialog import passphraseDialog


if not sys.platform.startswith('win'):
    import pexpect


class KeyDist():

    def complete(self):
        returnval = self.completed.isSet()
        return returnval


    class startAgentThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()


        def run(self):
            agentenv = None
            self.keydistObject.sshAgentProcess = None
            try:
                agentenv = os.environ['SSH_AUTH_SOCK']
            except:
                # If we start the agent, we will stop the agent.
                self.keydistObject.stopAgentOnExit.set()
                logger.debug(traceback.format_exc())
                try:
                    self.keydistObject.keyModel.startAgent()
                except Exception as e:
                    self.keydistObject.cancel(message="I tried to start an ssh agent, but failed with the error message %s" % str(e))
                    return

            newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_GETPUBKEY,self.keydistObject)
            if (not self.stopped()):
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),newevent)

    class loadkeyThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):
            logger.debug("loadkeyThread: started")
            try:
                logger.debug("loadkeyThread: Trying to open the key file")
                with open(self.keydistObject.keyModel.sshpaths.sshKeyPath,'r'): pass
                event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_LOADKEY,self.keydistObject)
            except Exception as e:
                logger.error("loadkeyThread: Failed to open the key file %s" % str(e))
                self.keydistObject.cancel("Failed to open the key file %s" % str(e))
                return
            if (not self.stopped()):
                logger.debug("loadkeyThread: generating LOADKEY event from loadkeyThread")
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),event)

    class genkeyThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):
            logger.debug("genkeyThread: started")
            if self.keydistObject.removeKeyOnExit.isSet():
                self.keydistObject.keyModel.deleteKey()
            self.nextEvent=None
            def success(): 
                self.nextEvent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_LOADKEY,self.keydistObject)
            def failure(): 
                self.keydistObject.cancel("Unable to generate a new key pair")
            self.keydistObject.keyModel.generateNewKey(self.keydistObject.password,success,failure,failure)
            self.keydistObject.keyCreated.set()
            if (not self.stopped() and self.nextEvent != None):
                logger.debug("genkeyThread: generating LOADKEY event from genkeyThread")
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),self.nextEvent)

    class getPubKeyThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):
            threadid = threading.currentThread().ident
            logger.debug("getPubKeyThread %i: started"%threadid)
            sshKeyListCmd = self.keydistObject.keyModel.sshpaths.sshAddBinary + " -L "
            logger.debug('getPubKeyThread: running command: ' + sshKeyListCmd)
            keylist = subprocess.Popen(sshKeyListCmd, stdout = subprocess.PIPE,stderr=subprocess.STDOUT,shell=True,universal_newlines=True)
            (stdout,stderr) = keylist.communicate()
            self.keydistObject.pubkeylock.acquire()

            logger.debug("getPubKeyThread %i: stdout of ssh-add -l: "%threadid + str(stdout))
            logger.debug("getPubKeyThread %i: stderr of ssh-add -l: "%threadid + str(stderr))

            lines = stdout.split('\n')
            logger.debug("getPubKeyThread %i: ssh key list completed"%threadid)
            for line in lines:
                match = re.search("^(?P<keytype>\S+)\ (?P<key>\S+)\ (?P<keycomment>.+)$",line)
                if match:
                    keycomment = match.group('keycomment')
                    if self.keydistObject.keyModel.isTemporaryKey():
                        correctKey = re.search('.*{launchercomment}.*'.format(launchercomment=self.keydistObject.keyModel.getPrivateKeyFilePath()),keycomment)
                    else:
                        correctKey = re.search('.*{launchercomment}.*'.format(launchercomment=self.keydistObject.keyModel.getLauncherKeyComment()),keycomment)
                    if correctKey:
                        self.keydistObject.keyloaded.set()
                        logger.debug("getPubKeyThread %i: loaded key successfully"%threadid)
                        self.keydistObject.pubkey = line.rstrip()
            logger.debug("getPubKeyThread %i: all lines processed"%threadid)
            if (self.keydistObject.keyloaded.isSet()):
                logger.debug("getPubKeyThread %i: key loaded"%threadid)
                logger.debug("getPubKeyThread %i: found a key, creating TESTAUTH event"%threadid)
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_TESTAUTH,self.keydistObject)
            else:
                logger.debug("getPubKeyThread %i: did not find a key, creating LOADKEY event"%threadid)
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_LOADKEY,self.keydistObject)
            self.keydistObject.pubkeylock.release()
            if (not self.stopped()):
                logger.debug("getPubKeyThread %i: is posting the next event"%threadid)
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),newevent)
            logger.debug("getPubKeyThread %i: stopped"%threadid)

    class scanHostKeysThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self.ssh_keygen_cmd = '{sshkeygen} -F {host} -f {known_hosts_file}'.format(sshkeygen=self.keydistObject.keyModel.sshpaths.sshKeyGenBinary,host=self.keydistObject.host,known_hosts_file=self.keydistObject.keyModel.sshpaths.sshKnownHosts)
            self.ssh_keyscan_cmd = '{sshscan} -H {host}'.format(sshscan=self.keydistObject.keyModel.sshpaths.sshKeyScanBinary,host=self.keydistObject.host)
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def getKnownHostKeys(self):
            keygen = subprocess.Popen(self.ssh_keygen_cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=True,universal_newlines=True)
            stdout,stderr = keygen.communicate()
            keygen.wait()
            hostkeys=[]
            for line in stdout.split('\n'):
                if (not (line.find('#')==0 or line == '')):
                    hostkeys.append(line)
            return hostkeys
                    
        def appendKey(self,key):
            with open(self.keydistObject.keyModel.sshpaths.sshKnownHosts,'a+') as known_hosts:
                known_hosts.write(key)
                known_hosts.write('\n')
            

        def scanHost(self):
            scan = subprocess.Popen(self.ssh_keyscan_cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=True,universal_newlines=True)
            stdout,stderr = scan.communicate()
            scan.wait()
            hostkeys=[]
            for line in stdout.split('\n'):
                if (not (line.find('#')==0 or line == '')):
                    hostkeys.append(line)
            return hostkeys

        def run(self):
            knownKeys = self.getKnownHostKeys()
            if (len(knownKeys)==0):
                hostKeys = self.scanHost()
                for key in hostKeys:
                    self.appendKey(key)
            newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_NEEDAGENT,self.keydistObject)
            if (not self.stopped()):
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),newevent)
                        
            

    class testAuthThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):
        
            # I have a problem where I have multiple identity files in my ~/.ssh, and I want to use only identities loaded into the agent
            # since openssh does not seem to have an option to use only an agent we have a workaround, 
            # by passing the -o IdentityFile option a path that does not exist, openssh can't use any other identities, and can only use the agent.
            # This is a little "racy" in that a tempfile with the same path could conceivably be created between the unlink and openssh attempting to use it
            # but since the pub key is extracted from the agent not the identity file I can't see anyway an attacker could use this to trick a user into uploading the attackers key.
            threadid = threading.currentThread().ident
            logger.debug("testAuthThread %i: started"%threadid)
            import tempfile, os
            (fd,path)=tempfile.mkstemp()
            os.close(fd)
            os.unlink(path)
            
            ssh_cmd = '{sshbinary} -o IdentityFile={nonexistantpath} -o PasswordAuthentication=no -o PubkeyAuthentication=yes -o StrictHostKeyChecking=no -l {login} {host} echo "success_testauth"'.format(sshbinary=self.keydistObject.keyModel.sshpaths.sshBinary,
                                                                                                                                                                                                             login=self.keydistObject.username,
                                                                                                                                                                                                             host=self.keydistObject.host,
                                                                                                                                                                                                             nonexistantpath=path)

            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            except:
                logger.debug(traceback.format_exc())
                # On non-Windows systems the previous block will die with 
                # "AttributeError: 'module' object has no attribute 'STARTUPINFO'" even though
                # the code is inside the 'if' block, hence the use of a dodgy try/except block.
                startupinfo = None

            logger.debug('testAuthThread: attempting: ' + ssh_cmd)
            ssh = subprocess.Popen(ssh_cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,shell=True,universal_newlines=True, startupinfo=startupinfo)
            stdout, stderr = ssh.communicate()
            ssh.wait()

            logger.debug("testAuthThread %i: stdout of ssh command: "%threadid + str(stdout))
            logger.debug("testAuthThread %i: stderr of ssh command: "%threadid + str(stderr))


            if 'Could not resolve hostname' in stdout:
                logger.debug('Network error.')
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_NETWORK_ERROR,self.keydistObject)
            elif 'success_testauth' in stdout:
                logger.debug("testAuthThread %i: got success_testauth in stdout :)"%threadid)
                self.keydistObject.authentication_success = True
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_AUTHSUCCESS,self.keydistObject)
            elif 'Agent admitted' in stdout:
                logger.debug("testAuthThread %i: the ssh agent has an error. Try rebooting the computer")
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_CANCEL,self.keydistObject,"Sorry, there is a problem with the SSH agent.\nThis sort of thing usually occurs if you delete your key and create a new one.\nThe easiest solution is to reboot your computer and try again.")
            else:
                logger.debug("testAuthThread %i: did not see success_testauth in stdout, posting EVT_KEYDIST_AUTHFAIL event"%threadid)
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_AUTHFAIL,self.keydistObject)

            if (not self.stopped()):
                logger.debug("testAuthThread %i: self.stopped() == False, so posting event: "%threadid + str(newevent))
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),newevent)
            logger.debug("testAuthThread %i: stopped"%threadid)


    class loadKeyThread(Thread):
        def __init__(self,keydistObject):
            Thread.__init__(self)
            self.keydistObject = keydistObject
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):

            self.nextEvent=None
            threadid=threading.currentThread().ident
            threadname=threading.currentThread().name
            km =self.keydistObject.keyModel
            if (self.keydistObject.password!=None):
                password=self.keydistObject.password
                newevent1 = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_KEY_WRONGPASS, self.keydistObject)
            else:
                newevent1 = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_KEY_LOCKED, self.keydistObject)
                password=""
            def incorrectCallback():
                self.nextEvent = newevent1
            def loadedCallback():
                self.nextEvent =KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_GETPUBKEY, self.keydistObject)
            def notFoundCallback():
                self.nextEvent=KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_NEWPASS_REQ,self.keydistObject)
            def failedToConnectToAgentCallback():
                self.nextEvent=KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_NEEDAGENT,self.keydistObject)
            km.addKeyToAgent(password,loadedCallback,incorrectCallback,notFoundCallback,failedToConnectToAgentCallback)
            if (not self.stopped() and self.nextEvent != None):
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(),self.nextEvent)


    class CopyIDThread(Thread):
        def __init__(self,keydist):
            Thread.__init__(self)
            self.keydistObject = keydist
            self._stop = Event()

        def stop(self):
            self._stop.set()
        
        def stopped(self):
            return self._stop.isSet()

        def run(self):
            sshClient = ssh.SSHClient()
            sshClient.set_missing_host_key_policy(ssh.AutoAddPolicy())
            try:
                sshClient.connect(hostname=self.keydistObject.host,username=self.keydistObject.username,password=self.keydistObject.password,allow_agent=False,look_for_keys=False)
                sshClient.exec_command("module load massive")
                sshClient.exec_command("/bin/mkdir -p ~/.ssh")
                sshClient.exec_command("/bin/chmod 700 ~/.ssh")
                sshClient.exec_command("/bin/touch ~/.ssh/authorized_keys")
                sshClient.exec_command("/bin/chmod 600 ~/.ssh/authorized_keys")
                sshClient.exec_command("/bin/echo \"%s\" >> ~/.ssh/authorized_keys"%self.keydistObject.pubkey)
                # FIXME The exec_commands above can fail if the user is over quota.
                sshClient.close()
                self.keydistObject.keycopied.set()
                event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_TESTAUTH,self.keydistObject)
                logger.debug('CopyIDThread: successfully copied the key')
            except socket.gaierror as e:
                logger.debug('CopyIDThread: socket.gaierror : ' + str(e))
                self.keydistObject.cancel(message=str(e))
                return
            except socket.error as e:
                logger.debug('CopyIDThread: socket.error : ' + str(e))
                if str(e) == '[Errno 101] Network is unreachable':
                    e = 'Network error, could not contact login host.'
                self.keydistObject.cancel(message=str(e))
                return
            except ssh.AuthenticationException as e:
                logger.debug('CopyIDThread: ssh.AuthenticationException: ' + str(e))
                event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_COPYID_NEEDPASS,self.keydistObject,str(e))
            except ssh.SSHException as e:
                logger.debug('CopyIDThread: ssh.SSHException : ' + str(e))
                self.keydistObject.cancel(message=str(e))
                return
            if (not self.stopped()):
                wx.PostEvent(self.keydistObject.notifywindow.GetEventHandler(), event)



    class sshKeyDistEvent(wx.PyCommandEvent):
        def __init__(self,id,keydist,arg=None):
            wx.PyCommandEvent.__init__(self,KeyDist.myEVT_CUSTOM_SSHKEYDIST,id)
            self.keydist = keydist
            self.arg = arg
            self.threadid = threading.currentThread().ident
            self.threadname = threading.currentThread().name

        def newkey(event):
            usingOneTimePassphrase = False
            if (event.GetId() == KeyDist.EVT_KEYDIST_NEWPASS_REQ):
                logger.debug("received NEWPASS_REQ event")
                if event.keydist.removeKeyOnExit.isSet():
                    usingOneTimePassphrase = True
                    import string
                    import random
                    oneTimePassphrase=''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for x in range(10))
                    logger.debug("sshKeyDistEvent.newkey: oneTimePassphrase: " + oneTimePassphrase)
                    event.keydist.password = oneTimePassphrase
                else:
                    wx.CallAfter(event.keydist.getPassphrase,event.arg)
            if (event.GetId() == KeyDist.EVT_KEYDIST_NEWPASS_COMPLETE or usingOneTimePassphrase):
                if event.GetId() == KeyDist.EVT_KEYDIST_NEWPASS_COMPLETE:
                    logger.debug("received NEWPASS_COMPLETE event")
                if usingOneTimePassphrase:
                    logger.debug("Using one-time passphrase.")
                t = KeyDist.genkeyThread(event.keydist)
                t.setDaemon(True)
                t.start()
                event.keydist.threads.append(t)
            event.Skip()

        def copyid(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_COPYID_NEEDPASS):
                logger.debug("received COPYID_NEEDPASS event")
                wx.CallAfter(event.keydist.getLoginPassword,event.arg)
            elif (event.GetId() == KeyDist.EVT_KEYDIST_COPYID):
                logger.debug("received COPYID event")
                t = KeyDist.CopyIDThread(event.keydist)
                t.setDaemon(True)
                t.start()
                event.keydist.threads.append(t)
            else:
                event.Skip()

        def scanhostkeys(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_SCANHOSTKEYS):
                logger.debug("received SCANHOSTKEYS event")
                t = KeyDist.scanHostKeysThread(event.keydist)
                t.setDaemon(True)
                t.start()
                event.keydist.threads.append(t)
            event.Skip()



        def shutdownEvent(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_SHUTDOWN):
                logger.debug("received EVT_KEYDIST_SHUTDOWN event")
                event.keydist.shutdownReal()
            else:
                event.Skip()

        def cancel(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_CANCEL):
                logger.debug("received EVT_KEYDIST_CANCEL event")
                event.keydist._canceled.set()
                event.keydist.shutdownReal()
                if event.arg!=None:
                    pass
                if (event.keydist.callback_fail != None):
                    event.keydist.callback_fail(event.arg)
            else:
                event.Skip()

        def success(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_AUTHSUCCESS):
                logger.debug("received AUTHSUCCESS event")
                event.keydist.completed.set()
                if (event.keydist.callback_success != None):
                    event.keydist.callback_success()
            event.Skip()


        def needagent(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_NEEDAGENT and not event.keydist.canceled()):
                logger.debug("received NEEDAGENT event")
                t = KeyDist.startAgentThread(event.keydist)
                t.setDaemon(True)
                t.start()
                event.keydist.threads.append(t)
            else:
                event.Skip()

        def listpubkeys(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_GETPUBKEY and not event.keydist.canceled()):
                t = KeyDist.getPubKeyThread(event.keydist)
                t.setDaemon(True)
                t.start()
                logger.debug("received GETPUBKEY event from thread %i %s, starting thread %i %s in response"%(event.threadid,event.threadname,t.ident,t.name))
                event.keydist.threads.append(t)
            else:
                event.Skip()

        def testauth(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_TESTAUTH):
                t = KeyDist.testAuthThread(event.keydist)
                t.setDaemon(True)
                t.start()
                logger.debug("received TESTAUTH event from thread %i %s, starting thread %i %s in response"%(event.threadid,event.threadname,t.ident,t.name))
                event.keydist.threads.append(t)
            else:
                event.Skip()

        def networkError(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_NETWORK_ERROR):
                event.keydist.cancel(message='Network error, could not contact login host.')
                return
            else:
                event.Skip()
            
        def keylocked(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_KEY_LOCKED):
                logger.debug("received KEY_LOCKED event")
                wx.CallAfter(event.keydist.GetKeyPassphrase)
            if (event.GetId() == KeyDist.EVT_KEYDIST_KEY_WRONGPASS):
                logger.debug("received KEY_WRONGPASS event")
                wx.CallAfter(event.keydist.GetKeyPassphrase,incorrect=True)
            event.Skip()

        def loadkey(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_LOADKEY and not event.keydist.canceled()):
                t = KeyDist.loadKeyThread(event.keydist)
                t.setDaemon(True)
                t.start()
                logger.debug("received LOADKEY event from thread %i %s, starting thread %i %s in response"%(event.threadid,event.threadname,t.ident,t.name))
                event.keydist.threads.append(t)
            else:
                event.Skip()

        def authfail(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_AUTHFAIL and not event.keydist.canceled()):
                if(not event.keydist.keyloaded.isSet()):
                    newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_LOADKEY,event.keydist)
                    wx.PostEvent(event.keydist.notifywindow.GetEventHandler(),newevent)
                else:
                    # If the key is loaded into the ssh agent, then authentication failed because the public key isn't on the server.
                    # *****TODO*****
                    # Actually this might not be strictly true. GNOME Keychain (and possibly others) will report a key loaded even if its still locked
                    # we probably need a button that says "I can't remember my old key's passphrase, please generate a new keypair"
                    if (event.keydist.keycopied.isSet()):
                        newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_TESTAUTH,event.keydist)
                        logger.debug("received AUTHFAIL event from thread %i %s posting TESTAUTH event in response"%(event.threadid,event.threadname))
                        wx.PostEvent(event.keydist.notifywindow.GetEventHandler(),newevent)
                    else:
                        newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_COPYID_NEEDPASS,event.keydist)
                        logger.debug("received AUTHFAIL event from thread %i %s posting NEEDPASS event in response"%(event.threadid,event.threadname))
                        wx.PostEvent(event.keydist.notifywindow.GetEventHandler(),newevent)
            else:
                event.Skip()


        def startevent(event):
            if (event.GetId() == KeyDist.EVT_KEYDIST_START):
                logger.debug("received KEYDIST_START event")
                newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_SCANHOSTKEYS,event.keydist)
                wx.PostEvent(event.keydist.notifywindow.GetEventHandler(),newevent)
            else:
                event.Skip()

    myEVT_CUSTOM_SSHKEYDIST=None
    EVT_CUSTOM_SSHKEYDIST=None
    def __init__(self,parentWindow,username,host,configName,notifywindow,keyModel,displayStrings=None,removeKeyOnExit=False):

        logger.debug("KeyDist.__init__")

        KeyDist.myEVT_CUSTOM_SSHKEYDIST=wx.NewEventType()
        KeyDist.EVT_CUSTOM_SSHKEYDIST=wx.PyEventBinder(self.myEVT_CUSTOM_SSHKEYDIST,1)
        KeyDist.EVT_KEYDIST_START = wx.NewId()
        KeyDist.EVT_KEYDIST_CANCEL = wx.NewId()
        KeyDist.EVT_KEYDIST_SHUTDOWN = wx.NewId()
        KeyDist.EVT_KEYDIST_SUCCESS = wx.NewId()
        KeyDist.EVT_KEYDIST_NEEDAGENT = wx.NewId()
        KeyDist.EVT_KEYDIST_NEEDKEYS = wx.NewId()
        KeyDist.EVT_KEYDIST_GETPUBKEY = wx.NewId()
        KeyDist.EVT_KEYDIST_TESTAUTH = wx.NewId()
        KeyDist.EVT_KEYDIST_AUTHSUCCESS = wx.NewId()
        KeyDist.EVT_KEYDIST_AUTHFAIL = wx.NewId()
        KeyDist.EVT_KEYDIST_NEWPASS_REQ = wx.NewId()
        KeyDist.EVT_KEYDIST_NEWPASS_RPT = wx.NewId()
        KeyDist.EVT_KEYDIST_NEWPASS_COMPLETE = wx.NewId()
        KeyDist.EVT_KEYDIST_COPYID = wx.NewId()
        KeyDist.EVT_KEYDIST_COPYID_NEEDPASS = wx.NewId()
        KeyDist.EVT_KEYDIST_KEY_LOCKED = wx.NewId()
        KeyDist.EVT_KEYDIST_KEY_WRONGPASS = wx.NewId()
        KeyDist.EVT_KEYDIST_SCANHOSTKEYS = wx.NewId()
        KeyDist.EVT_KEYDIST_LOADKEY = wx.NewId()
        KeyDist.EVT_KEYDIST_NETWORK_ERROR = wx.NewId()

        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.cancel)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.shutdownEvent)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.success)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.needagent)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.listpubkeys)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.testauth)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.authfail)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.startevent)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.newkey)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.copyid)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.keylocked)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.scanhostkeys)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.loadkey)
        notifywindow.Bind(self.EVT_CUSTOM_SSHKEYDIST, KeyDist.sshKeyDistEvent.networkError)

        self.completed=Event()
        self.parentWindow = parentWindow
        self.username = username
        self.host = host
        self.configName = configName
        self.displayStrings=displayStrings
        self.notifywindow = notifywindow
        self.sshKeyPath = ""
        self.threads=[]
        self.pubkeyfp = None
        self.keyloaded=Event()
        self.password = None
        self.pubkeylock = Lock()
        self.keycopied=Event()
        self.authentication_success = False
        self.callback_success=None
        self.callback_fail=None
        self.callback_error = None
        self._canceled=Event()
        self.removeKeyOnExit=Event()
        self.keyCreated=Event()
        if removeKeyOnExit:
            self.removeKeyOnExit.set()
        self.stopAgentOnExit=Event()
        self.keyModel = keyModel

    def GetKeyPassphrase(self,incorrect=False):
        if (incorrect):
            ppd = passphraseDialog(self.parentWindow,wx.ID_ANY,'Unlock Key',self.displayStrings.passphrasePromptIncorrect,"OK","Cancel")
        else:
            ppd = passphraseDialog(self.parentWindow,wx.ID_ANY,'Unlock Key',self.displayStrings.passphrasePrompt,"OK","Cancel")
        (canceled,passphrase) = ppd.getPassword()
        if (canceled):
            self.cancel("Sorry, I can't continue without the passphrase for that key. If you've forgotten the passphrase, you could remove the key and generate a new one. The key is probably located in ~/.ssh/MassiveLauncherKey*")
            return
        else:
            self.password = passphrase
            event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_TESTAUTH,self)
            wx.PostEvent(self.notifywindow.GetEventHandler(),event)


    def getLoginPassword(self,incorrect=False):
        if (not incorrect):
            ppd = passphraseDialog(self.parentWindow,wx.ID_ANY,'Login Password',self.displayStrings.passwdPrompt.format(**self.__dict__),"OK","Cancel")
        else:
            ppd = passphraseDialog(self.parentWindow,wx.ID_ANY,'Login Password',self.displayStrings.passwdPromptIncorrect.format(**self.__dict__),"OK","Cancel")
        (canceled,password) = ppd.getPassword()
        if canceled:
            self.cancel()
            return
        self.password = password
        event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_COPYID,self)
        wx.PostEvent(self.notifywindow.GetEventHandler(),event)

    def getPassphrase(self,reason=None):
        from CreateNewKeyDialog import CreateNewKeyDialog
        createNewKeyDialog = CreateNewKeyDialog(self.parentWindow, wx.ID_ANY, 'MASSIVE/CVL Launcher Private Key', self.displayStrings, displayMessageBoxReportingSuccess=False)
        canceled = createNewKeyDialog.ShowModal()==wx.ID_CANCEL
        if (not canceled):
            logger.debug("User didn't cancel from CreateNewKeyDialog.")
            self.password=createNewKeyDialog.getPassphrase()
            event = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_NEWPASS_COMPLETE,self)
            wx.PostEvent(self.notifywindow.GetEventHandler(),event)
        else:
            logger.debug("KeyDist.getPassphrase: User canceled from CreateNewKeyDialog.")
            self.cancel()

    def distributeKey(self,callback_success=None, callback_fail=None):
        event = KeyDist.sshKeyDistEvent(self.EVT_KEYDIST_START, self)
        wx.PostEvent(self.notifywindow.GetEventHandler(), event)
        self.callback_fail      = callback_fail
        self.callback_success   = callback_success
        
    def canceled(self):
        return self._canceled.isSet()

    def cancel(self,message=""):
        if (not self.canceled()):
            self._canceled.set()
            newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_CANCEL, self)
            newevent.arg = message
            logger.debug('Sending EVT_KEYDIST_CANCEL event.')
            wx.PostEvent(self.notifywindow.GetEventHandler(), newevent)

    def shutdownReal(self):

        if self.removeKeyOnExit.isSet():
            logger.debug("sshKeyDist.shutdownReal: removeKeyOnExit is set. Calling KeyModel.deleteKey and KeyModel.removeFromAgent")
            # TODO
            # These should be in their own thread. Both of these actions cause disk acceses.
            if self.keyCreated.isSet():
                t=threading.Thread(target=self.keyModel.deleteKey)
                t.start()
                self.threads.append(t)
            t=threading.Thread(target=self.keyModel.removeKeyFromAgent)
            t.start()
            self.threads.append(t)
            # TODO
            # delete the key from the server as well.
        else:
            logger.debug("sshKeyDist.shutdownReal: removeKeyOnExit is not set. No action taken")
        if self.stopAgentOnExit.isSet():
            logger.debug("sshKeyDist.shutdownReal: stopAgentOnExit is set, creating a thread to kill the agent")
            t=threading.Thread(target=self.keyModel.stopAgent)
            t.start()
            self.threads.append(t)
        else:
            logger.debug("sshKeyDist.shutdownReal: stopAgentOnExit is not set. No action taken")
        logger.debug("sshKeyDist.shutdownReal: calling stop and join on all threads")
        for t in self.threads:
            try:
                t.stop()
                t.join()
            except:
                pass
        self.completed.set()

    def shutdown(self):
        if (not self.canceled()):
            newevent = KeyDist.sshKeyDistEvent(KeyDist.EVT_KEYDIST_SHUTDOWN, self)
            logger.debug('Sending EVT_KEYDIST_SHUTDOWN event.')
            wx.PostEvent(self.notifywindow.GetEventHandler(), newevent)

