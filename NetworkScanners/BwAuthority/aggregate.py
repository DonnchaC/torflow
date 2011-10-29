#!/usr/bin/python
import os
import re
import math
import sys
import socket
import time
import traceback

sys.path.append("../../")
from TorCtl.TorUtil import plog
from TorCtl import TorCtl,TorUtil
from TorCtl.PathSupport import VersionRangeRestriction, NodeRestrictionList, NotNodeRestriction

bw_files = []
timestamps = {}
nodes = {}
prev_consensus = {}

# Hack to kill voting on guards while the network rebalances
IGNORE_GUARDS = 0

# The guard measurement period is based on the client turnover
# rate for guard nodes
GUARD_SAMPLE_RATE = 2*7*24*60*60 # 2wks

# PID constants
# See https://en.wikipedia.org/wiki/PID_controller#Ideal_versus_standard_PID_form
K_p = 1.0

# We expect to correct steady state error in 4 samples
T_i = 4

# We can only expect to predict less than one sample into the future, as
# after 1 sample, clients will have migrated
# FIXME: This is a function of the consensus time..
T_d = 0.5

K_i = K_p/T_i
K_d = K_p*T_d

NODE_CAP = 0.05

MIN_REPORT = 60 # Percent of the network we must measure before reporting

# Keep most measurements in consideration. The code below chooses
# the most recent one. 15 days is just to stop us from choking up 
# all the CPU once these things run for a year or so.
MAX_AGE = 60*60*24*15

# If the resultant scan file is older than 1.5 days, something is wrong
MAX_SCAN_AGE = 60*60*24*1.5


def base10_round(bw_val):
  # This keeps the first 3 decimal digits of the bw value only
  # to minimize changes for consensus diffs.
  # Resulting error is +/-0.5%
  if bw_val == 0:
    plog("INFO", "Zero input bandwidth.. Upping to 1")
    return 1
  else:
    ret = int(max((1000,
                   round(round(bw_val,-(int(math.log10(bw_val))-2)),
                                                       -3)))/1000)
    if ret == 0:
      plog("INFO", "Zero output bandwidth.. Upping to 1")
      return 1
    return ret



def closest_to_one(ratio_list):
  min_dist = 0x7fffffff
  min_item = -1
  for i in xrange(len(ratio_list)):
    if abs(1.0-ratio_list[i]) < min_dist:
      min_dist = abs(1.0-ratio_list[i])
      min_item = i
  return min_item

class NodeData:
  def __init__(self, timestamp):
    self.strm_bw = []
    self.filt_bw = []
    self.ns_bw = []
    self.desc_bw = []
    self.timestamp = timestamp

class Node:
  def __init__(self):
    self.node_data = {}
    self.ignore = False
    self.idhex = None
    self.nick = None
    self.chosen_time = None
    self.chosen_sbw = None
    self.chosen_fbw = None
    self.sbw_ratio = None
    self.fbw_ratio = None
    self.pid_error = 0
    self.prev_error = 0
    self.prev_voted_at = 0
    self.pid_error_sum = 0
    self.derror_dt = 0
    self.ratio = None
    self.new_bw = None
    self.change = None
    self.bw_idx = 0
    self.strm_bw = []
    self.filt_bw = []
    self.ns_bw = []
    self.desc_bw = []
    self.timestamps = []

  # Derivative of error for pid control
  def pid_bw(self, bw_idx, dt):
    return self.ns_bw[bw_idx] \
             + K_p*self.ns_bw[bw_idx]*self.pid_error \
             + K_i*self.ns_bw[bw_idx]*self.integral_error(dt) \
             + K_d*self.ns_bw[bw_idx]*self.d_error_dt(dt)

  # Time-weighted sum of error per unit of time, scaled
  # to arbitrary units of 'dt' seconds
  def integral_error(self, dt):
    return (self.pid_error_sum * GUARD_SAMPLE_RATE) / dt

  # Rate of error per unit of time, scaled to arbitrary 
  # units of 'dt' seconds
  def d_error_dt(self, dt):
    if self.prev_voted_at == 0 or self.prev_error == 0:
      self.derror_dt = 0
    else:
      self.derror_dt = ((dt*self.pid_error - dt*self.prev_error) /    \
                        (self.chosen_time - self.prev_voted_at))
    return self.derror_dt

  def add_line(self, line):
    if self.idhex and self.idhex != line.idhex:
      raise Exception("Line mismatch")
    self.idhex = line.idhex
    self.nick = line.nick
    if line.slice_file not in self.node_data \
      or self.node_data[line.slice_file].timestamp < line.timestamp:
      self.node_data[line.slice_file] = NodeData(line.timestamp)

    # FIXME: This is kinda nutty. Can we simplify? For instance,
    # do these really need to be lists inside the nd?
    nd = self.node_data[line.slice_file]
    nd.strm_bw.append(line.strm_bw)
    nd.filt_bw.append(line.filt_bw)
    nd.ns_bw.append(line.ns_bw)
    nd.desc_bw.append(line.desc_bw)

    self.strm_bw = []
    self.filt_bw = []
    self.ns_bw = []
    self.desc_bw = []
    self.timestamps = []

    for nd in self.node_data.itervalues():
      self.strm_bw.extend(nd.strm_bw)
      self.filt_bw.extend(nd.filt_bw)
      self.ns_bw.extend(nd.ns_bw)
      self.desc_bw.extend(nd.desc_bw)
      for i in xrange(len(nd.ns_bw)):
        self.timestamps.append(nd.timestamp)

  def avg_strm_bw(self):
    return sum(self.strm_bw)/float(len(self.strm_bw))

  def avg_filt_bw(self):
    return sum(self.filt_bw)/float(len(self.filt_bw))

  def avg_ns_bw(self):
    return sum(self.ns_bw)/float(len(self.ns_bw))

  def avg_desc_bw(self):
    return sum(self.desc_bw)/float(len(self.desc_bw))

  # This can be bad for bootstrapping or highly bw-variant nodes... 
  # we will choose an old measurement in that case.. We need
  # to build some kind of time-bias here..
  def _choose_strm_bw_one(self, net_avg):
    i = closest_to_one(map(lambda f: f/net_avg, self.strm_bw))
    self.chosen_sbw = i
    return self.chosen_sbw

  def _choose_filt_bw_one(self, net_avg):
    i = closest_to_one(map(lambda f: f/net_avg, self.filt_bw))
    self.chosen_fbw = i
    return self.chosen_fbw

  # Simply return the most recent one instead of this
  # closest-to-one stuff
  def choose_filt_bw(self, net_avg):
    max_idx = 0
    for i in xrange(len(self.timestamps)):
      if self.timestamps[i] > self.timestamps[max_idx]:
        max_idx = i
    self.chosen_fbw = max_idx
    return self.chosen_fbw

  def choose_strm_bw(self, net_avg):
    max_idx = 0
    for i in xrange(len(self.timestamps)):
      if self.timestamps[i] > self.timestamps[max_idx]:
        max_idx = i
    self.chosen_sbw = max_idx
    return self.chosen_sbw

class Line:
  def __init__(self, line, slice_file, timestamp):
    self.idhex = re.search("[\s]*node_id=([\S]+)[\s]*", line).group(1)
    self.nick = re.search("[\s]*nick=([\S]+)[\s]*", line).group(1)
    self.strm_bw = int(re.search("[\s]*strm_bw=([\S]+)[\s]*", line).group(1))
    self.filt_bw = int(re.search("[\s]*filt_bw=([\S]+)[\s]*", line).group(1))
    self.ns_bw = int(re.search("[\s]*ns_bw=([\S]+)[\s]*", line).group(1))
    self.desc_bw = int(re.search("[\s]*desc_bw=([\S]+)[\s]*", line).group(1))
    self.slice_file = slice_file
    self.timestamp = timestamp

class Vote:
  def __init__(self, line):
    # node_id=$DB8C6D8E0D51A42BDDA81A9B8A735B41B2CF95D1 bw=231000 diff=209281 nick=rainbowwarrior measured_at=1319822504
    self.idhex = re.search("[\s]*node_id=([\S]+)[\s]*", line).group(1)
    self.nick = re.search("[\s]*nick=([\S]+)[\s]*", line).group(1)
    self.bw = int(re.search("[\s]*bw=([\S]+)[\s]*", line).group(1))
    self.measured_at = int(re.search("[\s]*measured_at=([\S]+)[\s]*", line).group(1))
    try:
      self.pid_error = float(re.search("[\s]*pid_error=([\S]+)[\s]*", line).group(1))
      self.pid_error_sum = float(re.search("[\s]*pid_error_sum=([\S]+)[\s]*", line).group(1))
      self.vote_time = int(re.search("[\s]*vote_time=([\S]+)[\s]*", line).group(1))
    except:
      plog("NOTICE", "No previous PID data.")
      self.pid_error = 0
      self.pid_error_sum = 0
      self.vote_time = 0

class VoteSet:
  def __init__(self, filename):
    self.vote_map = {}
    try:
      f = file(filename, "r")
      f.readline()
      for line in f.readlines():
        vote = Vote(line)
        self.vote_map[vote.idhex] = vote
    except IOError:
      plog("NOTICE", "No previous vote data.")
      pass

# Misc items we need to get out of the consensus
class ConsensusJunk:
  def __init__(self, c):
    cs_bytes = c.sendAndRecv("GETINFO dir/status-vote/current/consensus\r\n")[0][2]
    self.bwauth_pid_control = False
    try:
      cs_params = re.search("^params ((?:[\S]+=[\d]+[\s]?)+)",
                                     cs_bytes, re.M).split()
      for p in cs_params:
        if p == "bwauthpid=1":
          self.bwauth_pid_control = True
    except:
      plog("NOTICE", "Bw auth PID control disabled due to parse error.")
      traceback.print_exc()

    self.bw_weights = {}
    try:
      bw_weights = re.search("^bandwidth-weights ((?:[\S]+=[\d]+[\s]?)+)",
                           cs_bytes, re.M).groups(1)[0].split()
      for b in bw_weights:
        pair = b.split("=")
        self.bw_weights[pair[0]] = int(pair[1])/10000.0
    except:
      plog("WARN", "No bandwidth weights in consensus!")
      self.bw_weights["Wgd"] = 0
      self.bw_weights["Wgg"] = 1.0

def main(argv):
  TorUtil.read_config(argv[1]+"/scanner.1/bwauthority.cfg")
  TorUtil.loglevel = "NOTICE"
 
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((TorUtil.control_host,TorUtil.control_port))
  c = TorCtl.Connection(s)
  c.debug(file(argv[1]+"/aggregate-control.log", "w", buffering=0))
  c.authenticate_cookie(file(argv[1]+"/tor/control_auth_cookie",
                         "r"))

  ns_list = c.get_network_status()
  for n in ns_list:
    if n.bandwidth == None: n.bandwidth = -1
  ns_list.sort(lambda x, y: y.bandwidth - x.bandwidth)
  for n in ns_list:
    if n.bandwidth == -1: n.bandwidth = None
  got_ns_bw = False
  max_rank = len(ns_list)

  cs_junk = ConsensusJunk(c)

  # FIXME: This is poor form.. We should subclass the Networkstatus class
  # instead of just adding members
  for i in xrange(max_rank):
    n = ns_list[i]
    n.list_rank = i
    if n.bandwidth == None:
      plog("NOTICE", "Your Tor is not providing NS w bandwidths for "+n.idhex)
    else:
      got_ns_bw = True
    n.measured = False
    prev_consensus["$"+n.idhex] = n

  if not got_ns_bw:
    # Sometimes the consensus lacks a descriptor. In that case,
    # it will skip outputting 
    plog("ERROR", "Your Tor is not providing NS w bandwidths!")
    sys.exit(0)

  # Take the most recent timestamp from each scanner 
  # and use the oldest for the timestamp of the result.
  # That way we can ensure all the scanners continue running.
  scanner_timestamps = {}
  for da in argv[1:-1]:
    # First, create a list of the most recent files in the
    # scan dirs that are recent enough
    for root, dirs, f in os.walk(da):
      for ds in dirs:
        if re.match("^scanner.[\d+]$", ds):
          newest_timestamp = 0
          for sr, sd, files in os.walk(da+"/"+ds+"/scan-data"):
            for f in files:
              if re.search("^bws-[\S]+-done-", f):
                fp = file(sr+"/"+f, "r")
                slicenum = sr+"/"+fp.readline()
                timestamp = float(fp.readline())
                fp.close()
                # old measurements are probably
                # better than no measurements. We may not
                # measure hibernating routers for days.
                # This filter is just to remove REALLY old files
                if time.time() - timestamp > MAX_AGE:
                  plog("DEBUG", "Skipping old file "+f)
                  # FIXME: Unlink this file + sql-
                  continue
                if timestamp > newest_timestamp:
                  newest_timestamp = timestamp
                bw_files.append((slicenum, timestamp, sr+"/"+f))
                # FIXME: Can we kill this?
                if slicenum not in timestamps or \
                     timestamps[slicenum] < timestamp:
                  timestamps[slicenum] = timestamp
          scanner_timestamps[ds] = newest_timestamp

  # Need to only use most recent slice-file for each node..
  for (s,t,f) in bw_files:
    fp = file(f, "r")
    fp.readline() # slicenum
    fp.readline() # timestamp
    for l in fp.readlines():
      try:
        line = Line(l,s,t)
        if line.idhex not in nodes:
          n = Node()
          nodes[line.idhex] = n
        else:
          n = nodes[line.idhex]
        n.add_line(line)
      except ValueError,e:
        plog("NOTICE", "Conversion error "+str(e)+" at "+l)
      except AttributeError, e:
        plog("NOTICE", "Slice file format error "+str(e)+" at "+l)
      except Exception, e:
        plog("WARN", "Unknown slice parse error "+str(e)+" at "+l)
        traceback.print_exc()
    fp.close()

  if len(nodes) == 0:
    plog("NOTICE", "No scan results yet.")
    sys.exit(1)
 
  pre_strm_avg = sum(map(lambda n: n.avg_strm_bw(), nodes.itervalues()))/ \
                  float(len(nodes))
  pre_filt_avg = sum(map(lambda n: n.avg_filt_bw(), nodes.itervalues()))/ \
                  float(len(nodes))

  plog("DEBUG", "Network pre_strm_avg: "+str(pre_strm_avg))
  plog("DEBUG", "Network pre_filt_avg: "+str(pre_filt_avg))

  for n in nodes.itervalues():
    n.choose_strm_bw(pre_strm_avg)
    n.choose_filt_bw(pre_filt_avg)
    plog("DEBUG", "Node "+n.nick+" chose sbw: "+\
                str(n.strm_bw[n.chosen_sbw])+" fbw: "+\
                str(n.filt_bw[n.chosen_fbw]))

  true_strm_avg = sum(map(lambda n: n.strm_bw[n.chosen_sbw],
                       nodes.itervalues()))/float(len(nodes))
  true_filt_avg = sum(map(lambda n: n.filt_bw[n.chosen_fbw],
                       nodes.itervalues()))/float(len(nodes))

  plog("DEBUG", "Network true_strm_avg: "+str(true_strm_avg))
  plog("DEBUG", "Network true_filt_avg: "+str(true_filt_avg))

  prev_votes = None
  if cs_junk.bwauth_pid_control:
    prev_votes = VoteSet(argv[-1])

    guard_cnt = 0
    node_cnt = 0
    guard_measure_time = 0
    node_measure_time = 0
    for n in nodes.itervalues():
      if n.idhex in prev_votes.vote_map and n.idhex in prev_consensus:
        if "Guard" in prev_consensus[n.idhex].flags:
          guard_cnt += 1
          guard_measure_time += (n.timestamps[n.chosen_fbw] - \
                                  prev_votes.vote_map[n.idhex].measured_at)
        else:
          node_cnt += 1
          node_measure_time += (n.timestamps[n.chosen_fbw] - \
                                  prev_votes.vote_map[n.idhex].measured_at)

  plog("INFO", "Average node measurement interval: "+str(node_measure_time/node_cnt))
  plog("INFO", "Average gaurd measurement interval: "+str(guard_measure_time/guard_cnt))

  # There is a difference between measure period and sample rate.
  # Measurement period is how fast the bandwidth auths can actually measure
  # the network. Sample rate is how often we want the PID feedback loop to
  # run. 
  NODE_SAMPLE_RATE = node_measure_time/node_cnt

  tot_net_bw = 0
  for n in nodes.itervalues():
    n.fbw_ratio = n.filt_bw[n.chosen_fbw]/true_filt_avg
    n.sbw_ratio = n.strm_bw[n.chosen_sbw]/true_strm_avg
    if n.sbw_ratio > n.fbw_ratio:
      # Does this ever happen?
      plog("NOTICE", "sbw > fbw for "+n.nick)
      n.ratio = n.sbw_ratio
      n.bw_idx = n.chosen_sbw
      n.pid_error = (n.strm_bw[n.chosen_sbw] - true_strm_avg)/true_strm_avg
    else:
      n.ratio = n.fbw_ratio
      n.bw_idx = n.chosen_fbw
      n.pid_error = (n.filt_bw[n.chosen_fbw] - true_filt_avg)/true_filt_avg

    n.chosen_time = n.timestamps[n.bw_idx]

    # XXX: What happens if we fall in and out of pid control due to ides
    # uptime issues or whatever
    if cs_junk.bwauth_pid_control:
      if n.idhex in prev_votes.vote_map:
        n.prev_error = prev_votes.vote_map[n.idhex].pid_error
        n.prev_voted_at = prev_votes.vote_map[n.idhex].vote_time
        # The integration here uses the measured values, not the vote/sample
        # values. Therefore, it requires the measure timespans
        n.pid_error_sum = prev_votes.vote_map[n.idhex].pid_error_sum + \
               n.pid_error*(n.chosen_time-prev_votes.vote_map[n.idhex].measured_at)/GUARD_SAMPLE_RATE

      # XXX: No reason to slow this down to NODE_SAMPLE_RATE...
      if n.chosen_time - prev_votes.vote_map[n.idhex].vote_time > NODE_SAMPLE_RATE:
        # Nodes with the Guard flag will respond
        # slowly to feedback. It must be applied less often,
        # and in proportion to the appropriate Wgx weight.
        if "Guard" in prev_consensus[n.idhex].flags:
          # Do full feedback if our previous vote > 2.5 weeks old
          if n.idhex not in prev_votes.vote_map or \
              n.chosen_time - prev_votes.vote_map[n.idhex].vote_Time > GUARD_SAMPLE_RATE:
            n.new_bw = n.pid_bw(GUARD_SAMPLE_RATE)
          else:
            # XXX: Update any of the n values based on this blend??
            guard_part = prev_votes.vote_map[n.idhex].bw # Use prev vote
            if "Exit" in prev_consensus[n.idhex].flags:
              n.new_bw = (1.0-cs_junk.bw_weights["Wgd"])*n.pid_bw(NODE_SAMPLE_RATE) + \
                        cs_junk.bw_weights["Wgd"]*guard_part
            else:
              n.new_bw = (1.0-cs_junk.bw_weights["Wgg"])*n.pid_bw(NODE_SAMPLE_RATE) + \
                        cs_junk.bw_weights["Wgg"]*guard_part
        else:
          # Everyone else should be pretty instantenous to respond.
          # Full feedback should be fine for them (we hope)
          n.new_bw = n.pid_bw(NODE_SAMPLE_RATE)
      else:
        # XXX: Reset any of the n values???
        if n.idhex in prev_votes.vote_map:
          n.new_bw = prev_votes.vote_map[n.idhex].bw
          n.vote_time = prev_votes.vote_map[n.idhex].vote_time
        else:
          # This should not happen.
          plog("WARN", "No previous vote for recent node "+n.nick+"="+n.idhex)
          n.new_bw = 0
          n.ignore = True
    else: # No PID feedback
      n.new_bw = n.desc_bw[n.bw_idx]*n.ratio

    n.change = n.new_bw - n.desc_bw[n.bw_idx]

    if n.idhex in prev_consensus:
      if prev_consensus[n.idhex].bandwidth != None:
        prev_consensus[n.idhex].measured = True
        tot_net_bw += n.new_bw
      if IGNORE_GUARDS \
           and ("Guard" in prev_consensus[n.idhex].flags and not "Exit" in \
                  prev_consensus[n.idhex].flags):
        plog("INFO", "Skipping voting for guard "+n.nick)
        n.ignore = True
      elif "Authority" in prev_consensus[n.idhex].flags:
        plog("INFO", "Skipping voting for authority "+n.nick)
        n.ignore = True

  # Go through the list and cap them to NODE_CAP
  for n in nodes.itervalues():
    if n.new_bw >= 0xffffffff*1000:
      plog("WARN", "Bandwidth of node "+n.nick+"="+n.idhex+" exceeded maxint32: "+str(n.new_bw))
      n.new_bw = 0xffffffff*1000
    if n.new_bw > tot_net_bw*NODE_CAP:
      plog("INFO", "Clipping extremely fast node "+n.idhex+"="+n.nick+
           " at "+str(100*NODE_CAP)+"% of network capacity ("
           +str(n.new_bw)+"->"+str(int(tot_net_bw*NODE_CAP))+")")
      n.new_bw = int(tot_net_bw*NODE_CAP)
      n.pid_error_sum = 0 # Don't let unused error accumulate...

  # WTF is going on here?
  oldest_timestamp = min(map(lambda n: n.chosen_time,
             filter(lambda n: n.idhex in prev_consensus,
                       nodes.itervalues())))
  plog("INFO", "Oldest measured node: "+time.ctime(oldest_timestamp))

  missed_nodes = 0.0
  for n in prev_consensus.itervalues():
    if not n.measured:
      if "Fast" in n.flags and "Running" in n.flags:
        try:
          r = c.get_router(n)
        except TorCtl.ErrorReply:
          r = None
        if r and not r.down and r.bw > 0:
          #if time.mktime(r.published.utctimetuple()) - r.uptime \
          #       < oldest_timestamp:
          missed_nodes += 1.0
          # We still tend to miss about 80 nodes even with these
          # checks.. Possibly going in and out of hibernation?
          plog("DEBUG", "Didn't measure "+n.idhex+"="+n.nickname+" at "+str(round((100.0*n.list_rank)/max_rank,1))+" "+str(n.bandwidth))

  measured_pct = round(100.0*len(nodes)/(len(nodes)+missed_nodes),1)
  if measured_pct < MIN_REPORT:
    plog("NOTICE", "Did not measure "+str(MIN_REPORT)+"% of nodes yet ("+str(measured_pct)+"%)")
    sys.exit(1)

  plog("INFO", "Measured "+str(measured_pct)+"% of all tor nodes.")

  n_print = nodes.values()
  n_print.sort(lambda x,y: int(y.change) - int(x.change))

  for scanner in scanner_timestamps.iterkeys():
    scan_age = int(round(scanner_timestamps[scanner],0))
    if scan_age < time.time() - MAX_SCAN_AGE:
      plog("WARN", "Bandwidth scanner "+scanner+" stale. Possible dead bwauthority.py. Timestamp: "+time.ctime(scan_age))

  out = file(argv[-1], "w")
  out.write(str(scan_age)+"\n")


  for n in n_print:
    if not n.ignore:
      out.write("node_id="+n.idhex+" bw="+str(base10_round(n.new_bw))+" diff="+str(int(round(n.change/1000.0,0)))+ " nick="+n.nick+ " measured_at="+str(int(n.chosen_time))+" pid_error="+str(n.pid_error)+" pid_error_sum="+str(n.pid_error_sum)+" derror_dt="+str(n.derror_dt)+" vote_time="+str(n.vote_time)+"\n")
  out.close()
 
if __name__ == "__main__":
  try:
    main(sys.argv)
  except socket.error, e:
    traceback.print_exc()
    plog("WARN", "Socket error. Are the scanning Tors running?")
    sys.exit(1)
  except Exception, e:
    plog("ERROR", "Exception during aggregate: "+str(e))
    traceback.print_exc()
    sys.exit(1)
  sys.exit(0)
