from cloud_utils.net_utils.sshconnection import SshConnection
from cloud_utils.log_utils import red, green, blue
from cloud_utils.log_utils.eulogger import Eulogger
from cloud_utils.log_utils import get_traceback, get_terminal_size
import argparse
import re
from socket import inet_aton
import struct
import  time
from threading import Thread, Lock
from Queue import Queue, Empty
from prettytable import PrettyTable

class RemoteCommands(object):
    """
    Utility to run commands on remote machines via ssh in batches.
    """
    
    def __init__(self, hostfile=None, ips=None, password=None, username='root',
                 command='echo "ALIVE', timeout=5, thread_count=20, log_level='debug'):

        self.parser = argparse.ArgumentParser(
            description='Run a command on list of remote hosts',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        self.parser.add_argument('-f', '--hostfile', default=hostfile,
                            help='file with list of ips and/or hostnames')
        self.parser.add_argument('-i', '--ips', default=ips,
                            help='comma or space separated list of ips and/or hostnames')
        self.parser.add_argument('-p', '--password', default=password,
                            help='Ssh password used to connect to hosts')
        self.parser.add_argument('-u', '--username', default=username,
                            help='Ssh username used to connect to hosts')
        self.parser.add_argument('-c', '--command', default=command,
                            help='file with list of ips and/or hostnames')
        self.parser.add_argument('-t', '--timeout', default=timeout, type=int,
                            help='Ssh connection timeout in seconds')
        self.parser.add_argument('--thread-count', default=thread_count, type=int,
                            help='Number of threads used to run commands on hosts')
        self.parser.add_argument('-l', '--log-level', default=log_level,
                            help='Loglevel')
        if ips or hostfile:
            args = ""
        else:
            args = None
        self.args = self.parser.parse_args(args=args)
        self.hostfile = self.args.hostfile
        self.password = self.args.password
        self.username = self.args.username
        self.command = self.args.command
        self.timeout = self.args.timeout
        self.log_level = self.args.log_level
        self.results = {}
        self.maxwait = .5
        self.ips = ips or self.args.ips
        self.logger = Eulogger('RemoteCmds', stdout_level=self.log_level)
        if self.ips:
            if isinstance(self.ips, basestring):
                self.ips = str(self.ips).replace(',', ' ')
                self.ips = self.ips.split()
            else:
                self.ips = list(self.ips)
        if self.args.hostfile:
            with open(hostfile) as f:
                self.ips.extend(f.readlines())
        if not self.ips:
            raise ValueError('No hosts provided. Use --hostfile or --ips to provide hosts to run '
                             'command against')

    def do_ssh(self, q, lock, name, command):
        empty = False
        q = None
        while not empty:
            try:
                ssh = None
                logger = None
                self.logger.debug('Thread: {0}, in Q loop...'.format(name))
                host = None
                try:
                    host = q.get(timeout=self.maxwait)
                except Empty:
                    empty = True
                    break
                start = time.time()
                try:
                    self.logger.debug('Connecting to new host:' + str(host))
                    logger = Eulogger(str(host))
                    ssh = SshConnection(host=host, username=self.username, password=self.password,
                                        debug_connect=True, timeout=self.args.timeout, verbose=True,
                                        logger=logger)
                    logger.debug('host: {0} running command:{1} '.format(host, command))
                    out = ssh.cmd(str(command), listformat=True)
                    logger.debug('Done with host: {0}'.format(host))
                    elapsed = int(time.time() - start)
                    with lock:
                        self.results[host] = {'status': out.get('status'),
                                              'output': out.get('output'),
                                              'elapsed': elapsed}
                except Exception as E:
                    elapsed = int(time.time() - start)
                    with lock:
                        self.results[host] = {'status': -1, 'output': [str(E)],
                                         'elapsed': elapsed}
                finally:
                    logger.debug('Closing ssh to host: {0}'.format(host))
                    if ssh:
                        ssh.connection.close()
                        logger.debug('Closed ssh to host: {0}'.format(host))
                    try:
                        if logger:
                            logger.close()
                    except:
                        pass
            except Exception as SE:
                self.logger.error('{0}\nError in do_ssh:{0}'.format(get_traceback(), SE))
            finally:
                if q is not None:
                    q.task_done()
                self.logger.debug('Finished task in thread:{0}'.format(name))
        self.logger.debug('{0}: Done with thread'.format(name))

    def run_remote_commands(self, ips=None, command=None, ):
        command = command or self.command
        iq = Queue()
        ips = ips or self.ips
        if not ips:
            raise ValueError('run_remote_commands: IP list was empty:"{0}"'.format(ips))
        for ip in ips:
            ip = str(ip).strip().rstrip()
            iq.put(ip)
        tlock = Lock()
        threadcount = self.args.thread_count
        if threadcount  > iq.qsize():
            threadcount = iq.qsize()
        if not iq:
            return
        self.results = {}
        for i in range(threadcount):
             t = Thread(target=self.do_ssh, args=(iq, tlock, i, command))
             t.daemon = True
             t.start()
        self.logger.debug('Threads started now waiting for join')
        iq.join()
        self.logger.debug('Done with join')
        time.sleep(self.maxwait + .1)
        return self.results

    def show_results(self, results=None, max_width=None, printmethod=None):
        results = results or self.results
        if not max_width:
            max_height, max_width = get_terminal_size()
        host_w = 24
        res_w = 4
        time_w = 6
        max_width = max_width - (host_w + res_w + time_w) - 5
        output_hdr = "OUTPUT"
        pt = PrettyTable(['HOST', 'RES', 'TIME', output_hdr])
        pt.align = 'l'
        pt.hrules = 1
        pt.padding_width = 0
        max_width = max_width - (host_w + res_w + time_w)
        pt.max_width[output_hdr] = max_width
        def sort_meth(ip):
            try:
                if re.match("^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"):
                    return struct.unpack("!L", inet_aton(ip))[0]
            except:
                pass
            return ip

        for host in sorted(results, key=sort_meth):
            result = self.results.get(host)
            output = ""
            for line in result.get('output'):
                line.rstrip()
                for x in xrange(0, len(line), max_width - 1):
                    part = str('{output: <{length}}'.format(output=line[x:(x + max_width - 1)],
                                                            length=max_width))
                    output += part
            status = result.get('status')
            if status == 0:
                color = green
            else:
                color = red
            pt.add_row([blue(host), color(result.get('status')), color(result.get('elapsed')),
                        color(output)])
        buf = "\n{0}\n".format(pt)
        if printmethod:
            printmethod(buf)
        else:
            print buf


if __name__ == "__main__":
    rm = RemoteCommands()
    rm.run_remote_commands()
    rm.show_results()
