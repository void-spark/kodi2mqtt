# -*- coding: utf-8 -*-

import xbmc,xbmcaddon
import json
import threading
import time
import os
import socket
from lib import client as mqtt

__addon__      = xbmcaddon.Addon()
__version__    = __addon__.getAddonInfo('version')

def getSetting(setting):
    return __addon__.getSetting(setting).strip()

def load_settings():
    global mqttprogress,mqttinterval,mqttdetails,mqttignore
    mqttprogress = getSetting('mqttprogress').lower() == "true"
    mqttinterval = int(getSetting('mqttinterval'))
    mqttdetails = getSetting('mqttdetails').lower() == "true"
    mqttignore = getSetting('mqttignore')
    if mqttignore:
        mqttignore = mqttignore.lower().split(',')

activeplayerid=-1
activeplayertype=""
playbackstate=0
lasttitle=""
lastdetail={}

#
# Returns true when no words are found, false on one or more matches
#
def ignorelist(data,val):
    if val == "filepath":
        val=xbmc.Player().getPlayingFile()
    return all(val.lower().find (v.strip()) <= -1 for v in data)

def mqttlogging(log):
    if  __addon__.getSetting("mqttdebug")=='true':
        xbmc.log(log,level=xbmc.LOGINFO)

def sendrpc(method,params):
    res=xbmc.executeJSONRPC(json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}))
    mqttlogging("MQTT: JSON-RPC call "+method+" returned "+res)
    return json.loads(res)

def setvol(data):
    params=json.loads('{"volume":' + str(data) + '}')
    sendrpc("Application.SetVolume",params)
    #res=xbmc.executebuiltin("XBMC.SetVolume("+data+")")
    xbmc.log(data)
#
# Publishes a MQTT message. The topic is built from the configured
# topic prefix and the suffix. The message itself is JSON encoded,
# with the "val" field set, and possibly more fields merged in.
#
def publish(suffix,val,more):
    global topic,mqc
    robj={}
    robj["val"]=val
    if more is not None:
        robj.update(more)
    jsonstr=json.dumps(robj)
    fulltopic=topic+"status/"+suffix
    mqttlogging("MQTT: Publishing @"+fulltopic+": "+jsonstr)
    mqc.publish(fulltopic,jsonstr,qos=0,retain=True)

#
# Set and publishes the playback state. Publishes more info if
# the state is "playing"
#
def setplaystate(state,detail):
    global activeplayerid,activeplayertype,playbackstate
    playbackstate=state
    if state==1:
        res=sendrpc("Player.GetActivePlayers",{})
        activeplayerid=res["result"][0]["playerid"]
        activeplayertype=res["result"][0]["type"]
        if mqttdetails and ignorelist(mqttignore,"filepath"):
            res=sendrpc("Player.GetProperties",{"playerid":activeplayerid,"properties":["speed","currentsubtitle","currentaudiostream","repeat","subtitleenabled"]})
            publish("playbackstate",state,{"kodi_state":detail,"kodi_playbackdetails":res["result"],"kodi_playerid":activeplayerid,"kodi_playertype":activeplayertype,"kodi_timestamp":int(time.time())})
            publishdetails()
        else:
            publish("playbackstate",state,{"kodi_state":detail,"kodi_playerid":activeplayerid,"kodi_playertype":activeplayertype,"kodi_timestamp":int(time.time())})
    else:
        publish("playbackstate",state,{"kodi_state":detail,"kodi_playerid":activeplayerid,"kodi_playertype":activeplayertype,"kodi_timestamp":int(time.time())})

def convtime(ts):
    return("%02d:%02d:%02d" % (ts/3600,(ts/60)%60,ts%60))

#
# Publishes playback progress
#
def publishprogress():
    global player
    if not player.isPlaying():
        return
    pt=player.getTime()
    tt=player.getTotalTime()
    if pt<0:
        pt=0
    if tt>0:
        progress=(pt*100)/tt
    else:
        progress=0
    state={"kodi_time":convtime(pt),"kodi_totaltime":convtime(tt)}
    publish("progress","%.1f" % progress,state)
    # Publish title at interval, this is for things like radio streams that change the title without notification
    title=xbmc.getInfoLabel('Player.Title')
    publish("playertitle",title,{})

#
# Publish more details about the currently playing item
#

def publishdetails():
    global player,activeplayerid
    global lasttitle,lastdetail
    if not player.isPlaying():
        return
    if ignorelist(mqttignore,"filepath"):
        res=sendrpc("Player.GetItem",{"playerid":activeplayerid,"properties":["title","streamdetails","file","thumbnail","fanart"]})
        if "result" in res:
            newtitle=res["result"]["item"]["title"]
            newdetail={"kodi_details":res["result"]["item"]}
            if newtitle!=lasttitle or newdetail!=lastdetail:
                lasttitle=newtitle
                lastdetail=newdetail
                if ignorelist(mqttignore,newtitle):
                    publish("title",newtitle,newdetail)
    if mqttprogress:
        publishprogress()

#
# Notification subclasses
#
class MQTTMonitor(xbmc.Monitor):
    def onSettingsChanged(self):
        global mqc
        mqttlogging("MQTT: Settings changed, reconnecting broker")
        mqc.loop_stop(True)
        load_settings()
        startmqtt()

    def onNotification(self,sender,method,data):
        publish("notification/"+method,data,None)

        # fix for netflixaddon - so that start notification 
        try:
            if method == 'Player.OnAVStart':
                setplaystate(1,"started")
        except Exception:
            import traceback
            mqttlogging("MQTT: "+traceback.format_exc())

    def onScreensaverActivated(self):
        publish("screensaver",1,"")

    def onScreensaverDeactivated(self):
        publish("screensaver",0,"")

class MQTTPlayer(xbmc.Player):

    def onAVStarted(self):
        setplaystate(1, "started")

    def onPlayBackPaused(self):
        setplaystate(2,"paused")

    def onPlayBackResumed(self):
        setplaystate(1,"resumed")

    def onPlayBackEnded(self):
        setplaystate(0,"ended")

    def onPlayBackStopped(self):
        setplaystate(0,"stopped")

    def onPlayBackSeek(self, time, seek_offset):
        publishprogress()

    def onPlayBackSeekChapter(self, chapter):
        publishprogress()

    def onPlayBackSpeedChanged(self,speed):
        setplaystate(1,"speed")

    def onQueueNextItem(self):
        mqttlogging("MQTT onqn")

#
# Handles commands
#
def processnotify(data):
    try:
        params=json.loads(data)
    except ValueError:
        parts = data.split(None, 1)
        params={"title":parts[0],"message":parts[1]}
    sendrpc("GUI.ShowNotification",params)

def processplay(data):
    try:
        params=json.loads(data)
        sendrpc("Player.Open",params)
    except ValueError:
        player.play(data)

def processvolume(data):
    try:
        vol = int(data)
        setvol(vol)
    except ValueError:
        params=json.loads(data)
        sendrpc("Application.SetVolume",params)

def processplaybackstate(data):
    global playbackstate
    if data=="0" or data=="stop":
        player.stop()
    elif data=="1" or data=="resume" or data=="play":
        if playbackstate==2:
            player.pause()
        elif playbackstate!=1:
            player.play()
    elif data=="2" or data=="pause":
    	if playbackstate==1:
            player.pause()
    elif data=="toggle":
    	if playbackstate==1 or playbackstate==2:
            player.pause()
    elif data=="next":
        player.playnext()
    elif data=="previous":
        player.playprevious()
    elif data=="playcurrent":
        path = xbmc.getInfoLabel('ListItem.FileNameAndPath')
        sendrpc("Player.Open", {"item": {"file": path}})

def processprogress(data):
    hours, minutes, seconds = [int(i) for i in data.split(":")]
    time = hours * 3600 + minutes * 60 + seconds
    player.seekTime(time)

def processsendcomand(data):
    try:
        cmd=json.loads(data)
        res=xbmc.executeJSONRPC(json.dumps(cmd))
        mqttlogging("MQTT: JSON-RPC call "+cmd['method']+" returned "+res)
    except ValueError:
        mqttlogging("MQTT: JSON-RPC call ValueError")

def processcecstate(data):
	if data=="1" or data=="activate":
		#Stupid workaround to wake TV
		mqttlogging("CEC Activate")
		os.system('kodi-send --action=""')

def processcommand(topic,data):
    mqttlogging("MQTT: Received command %s with data %s" % (topic, data))
    if topic=="notify":
        processnotify(data)
    elif topic=="play":
        processplay(data)
    elif topic=="playbackstate":
        processplaybackstate(data)
    elif topic=="progress":
        processprogress(data)
    elif topic=="api":
        processsendcomand(data)
    elif topic=="volume":
        processvolume(data)
    elif topic=="cecstate":
        processcecstate(data)
    else:
        mqttlogging("MQTT: Unknown command "+topic)

#
# Handles incoming MQTT messages
#
def msghandler(mqc,userdata,msg):
    try:
        global topic
        if msg.retain:
            return
        mytopic=msg.topic[len(topic):]
        if mytopic.startswith("command/"):
            processcommand(mytopic[8:],msg.payload.decode("utf-8"))
    except Exception as e:
        mqttlogging("MQTT: Error processing message %s: %s" % (type(e).__name__,e))

def connecthandler(mqc,userdata,flags,rc):
    mqttlogging("MQTT: Connected to MQTT broker with rc=%d" % (rc))
    mqc.publish(topic+"connected",2,qos=1,retain=True)
    mqc.subscribe(topic+"command/#",qos=0)

def disconnecthandler(mqc,userdata,rc):
    mqttlogging("MQTT: Disconnected from MQTT broker with rc=%d" % (rc))
    time.sleep(5)
    try:
        mqc.reconnect()
    except Exception as e:
        mqttlogging("MQTT: Error while reconnectig: message %s: %s" % (type(e).__name__,e))

#
# Starts connection to the MQTT broker, sets the will
# and subscribes to the command topic
#
def startmqtt():
    global topic,mqc
    mqc=mqtt.Client()
    mqc.on_message=msghandler
    mqc.on_connect=connecthandler
    mqc.on_disconnect=disconnecthandler
    if __addon__.getSetting("mqttanonymousconnection")=='false':
        mqc.username_pw_set(__addon__.getSetting("mqttusername"), __addon__.getSetting("mqttpassword"))
        mqttlogging("MQTT: Anonymous disabled, connecting as user: %s" % __addon__.getSetting("mqttusername"))
    if __addon__.getSetting("mqtttlsconnection")=='true' and  __addon__.getSetting("mqtttlsconnectioncrt")!='' and __addon__.getSetting("mqtttlsclient")=='false':
        mqc.tls_set(__addon__.getSetting("mqtttlsconnectioncrt"))
        mqttlogging("MQTT: TLS enabled, connecting using CA certificate: %s" % __addon__.getSetting("mqtttlsconnectioncrt"))
    elif __addon__.getSetting("mqtttlsconnection")=='true' and  __addon__.getSetting("mqtttlsclient")=='true' and __addon__.getSetting("mqtttlsclientcrt")!='' and  __addon__.getSetting("mqtttlsclientkey")!='':
        mqc.tls_set(__addon__.getSetting("mqtttlsconnectioncrt"), __addon__.getSetting("mqtttlsclientcrt"), __addon__.getSetting("mqtttlsclientkey"))
        mqttlogging("MQTT: TLS with client certificates enabled, connecting using certificates CA: %s, client %s and key: %s" % (__addon__.getSetting("mqttusername"), __addon__.getSetting("mqtttlsclientcrt"), __addon__.getSetting("mqtttlsclientkey")))
    topic=__addon__.getSetting("mqtttopic")
    if not topic.endswith("/"):
        topic+="/"
    mqc.will_set(topic+"connected",0,qos=2,retain=True)
    sleep=2
    for attempt in range(10):
        try:
            mqttlogging("MQTT: Connecting to MQTT broker at %s:%s" % (__addon__.getSetting("mqtthost"),__addon__.getSetting("mqttport")))
            mqc.connect(__addon__.getSetting("mqtthost"),int(__addon__.getSetting("mqttport")),60)
        except socket.error:
            mqttlogging("MQTT: Socket error raised, retry in %d seconds" % sleep)
            monitor.waitForAbort(sleep)
            sleep=sleep*2
        else:
            break
    else:
        mqttlogging("MQTT: No connection possible, giving up")
        return(False)
    mqc.loop_start()
    return(True)

#
# Addon initialization and shutdown
#
if (__name__ == "__main__"):
    global monitor,player
    mqttlogging('MQTT: MQTT Adapter Version %s started' % __version__)
    load_settings()
    monitor=MQTTMonitor()
    if startmqtt():
        player=MQTTPlayer()
        # Publish a reasonable initial state. Fancier would be to check actual current state.
        setplaystate(0,"stopped")
        if mqttprogress:
            mqttlogging("MQTT: Progress Publishing enabled, interval is set to %d seconds" % mqttinterval)
            while not monitor.waitForAbort(mqttinterval):
                publishprogress()
        else:
            mqttlogging("MQTT: Progress Publishing disabled, waiting for abort")
            monitor.waitForAbort()
        mqc.loop_stop(True)
    mqttlogging("MQTT: Shutting down")
