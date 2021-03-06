# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License version 3 for
# more details.
#
# You should have received a copy of the GNU General Public License version 3
# along with this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# (c) 2015-2016 Valentin Samir
import os
import sys
import socket
import time
import select
import traceback

from policyd_rate_limit import utils
from policyd_rate_limit.utils import config


class Pass(Exception):
    pass


class Policyd(object):
    """The policy server class"""
    socket_data_read = {}
    socket_data_write = {}

    def socket(self):
        """initialize the socket from the config parameters"""
        # if socket is a string assume it is the path to an unix socket
        if isinstance(config.SOCKET, str):
            try:
                os.remove(config.SOCKET)
            except OSError:
                if os.path.exists(config.SOCKET):  # pragma: no cover (should not happen)
                    raise
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # else asume its a tuple (bind_ip, bind_port)
        elif ':' in config.SOCKET[0]:  # assume ipv6 bind addresse
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        elif '.' in config.SOCKET[0]:  # assume ipv4 bind addresse
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            raise ValueError("bad socket %s" % (config.SOCKET,))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock = sock

    def close_socket(self):
        """close the socket depending of the config parameters"""
        self.sock.close()
        # if socket was an unix socket, delete it after closing.
        if isinstance(config.SOCKET, str):
            try:
                os.remove(config.SOCKET)
            except OSError as error:  # pragma: no cover (should not happen)
                sys.stderr.write("%s\n" % error)
                sys.stderr.flush()

    def close_connection(self, connection):
        """close a connection and clean read/write dict"""
        # Clean up the connection
        try:
            del self.socket_data_read[connection]
        except KeyError:
            pass
        try:
            del self.socket_data_write[connection]
        except KeyError:
            pass
        connection.close()

    def run(self):
        """The main server loop"""
        try:
            sock = self.sock
            sock.bind(config.SOCKET)
            if isinstance(config.SOCKET, str):
                os.chmod(config.SOCKET, config.socket_permission)
            sock.listen(5)
            self.socket_data_read[sock] = []
            if config.debug:
                sys.stderr.write('waiting for connections\n')
                sys.stderr.flush()
            while True:
                # wait for a socket to read to or to write to
                (rlist, wlist, _) = select.select(
                    self.socket_data_read.keys(), self.socket_data_write.keys(), []
                )
                for socket in rlist:
                    # if the socket is the main socket, there is a new connection to accept
                    if socket == sock:
                        connection, client_address = sock.accept()
                        if config.debug:
                            sys.stderr.write('connection from %s\n' % (client_address,))
                            sys.stderr.flush()
                        self.socket_data_read[connection] = []
                    # else there is data to read on a client socket
                    else:
                        self.read(socket)
                for socket in wlist:
                    try:
                        data = self.socket_data_write[socket]
                        sent = socket.send(data)
                        data_not_sent = data[sent:]
                        if data_not_sent:
                            self.socket_data_write[socket] = data_not_sent
                        else:
                            self.close_connection(socket)
                    # the socket has been closed during read
                    except KeyError:
                        pass
        except (KeyboardInterrupt, utils.Exit):
            for socket in list(self.socket_data_read.keys()):
                if socket != self.sock:
                    self.close_connection(sock)
            raise

    def read(self, connection):
        """Called then a connection is ready for reads"""
        try:
            # get the current buffer of the connection
            buffer = self.socket_data_read[connection]
            # read data
            data = connection.recv(1024).decode('UTF-8')
            if not data:
                raise ValueError("connection closed")
            if config.debug:
                sys.stderr.write(data)
                sys.stderr.flush()
            # accumulate it in buffer
            buffer.append(data)
            # if data len too short to determine if we are on an empty line, we
            # concatene datas in buffer
            if len(data) < 2:
                data = u"".join(buffer)
                buffer = [data]
            # We reach on empty line so the client has finish to send and wait for a response
            if data[-2:] == "\n\n":
                data = u"".join(buffer)
                request = {}
                # read data are like one key=value per line
                for line in data.split("\n"):
                    line = line.strip()
                    try:
                        key, value = line.split(u"=", 1)
                        if value:
                            request[key] = value
                    # if value is empty, ignore it
                    except ValueError:
                        pass
                # process the collected data in the action method
                self.action(connection, request)
            else:
                self.socket_data_read[connection] = buffer
        except (KeyboardInterrupt, utils.Exit):
            self.close_connection(connection)
            raise
        except Exception as error:
            traceback.print_exc()
            sys.stderr.flush()
            self.close_connection(connection)

    def action(self, connection, request):
        """Called then the client has sent an empty line"""
        id = None
        # By default, we do not block emails
        action = config.success_action
        try:
            if not config.database_is_initialized:
                utils.database_init()
            with utils.cursor() as cur:
                try:
                    # only care if the protocol states is RCTP. If the policy delegation in postfix
                    # configuration is in smtpd_recipient_restrictions as said in the doc,
                    # possible states are RCPT and VRFY.
                    if 'protocol_state' in request and request['protocol_state'].upper() != "RCPT":
                        raise Pass()
                    # if user is authenticated, we filter by sasl username
                    if config.limit_by_sasl and u'sasl_username' in request:
                        id = request[u'sasl_username']
                    # else, if activated, we filter by sender
                    elif config.limit_by_sender and u'sender' in request:
                        id = request[u'sender']
                    # else, if activated, we filter by ip source addresse
                    elif (
                        config.limit_by_ip and
                        u'client_address' in request and
                        utils.is_ip_limited(request[u'client_address'])
                    ):
                        id = request[u'client_address']
                    # if the client neither send us client ip adresse nor sasl username, jump
                    # to the next section
                    else:
                        raise Pass()
                    # Here we are limiting against sasl username, sender or source ip addresses.
                    # for each limit periods, we count the number of mails already send.
                    # if the a limit is reach, we change action to fail (deny the mail).
                    for mail_nb, delta in config.limits_by_id.get(id, config.limits):
                        cur.execute(
                            (
                                "SELECT COUNT(*) FROM mail_count "
                                "WHERE id = %s AND date >= %s"
                            ) % ((config.format_str,)*2),
                            (id, int(time.time() - delta))
                        )
                        nb = cur.fetchone()[0]
                        if config.debug:
                            sys.stderr.write("%03d/%03d hit since %ss\n" % (nb, mail_nb, delta))
                            sys.stderr.flush()
                        if nb >= mail_nb:
                            action = config.fail_action
                            if config.report and delta in config.report_limits:
                                utils.hit(cur, delta, id)
                            raise Pass()
                except Pass:
                    pass
                # If action is a success, record in the database that a new mail has just been sent
                if action == config.success_action and id is not None:
                    if config.debug:
                        sys.stderr.write(u"insert id %s\n" % id)
                        sys.stderr.flush()
                    cur.execute(
                        "INSERT INTO mail_count VALUES (%s, %s)" % ((config.format_str,)*2),
                        (id, int(time.time()))
                    )
        except utils.cursor.backend_module.Error as error:
            utils.cursor.del_db()
            action = config.db_error_action
            sys.stderr.write("Database error: %r\n" % error)
        data = u"action=%s\n\n" % action
        if config.debug:
            sys.stderr.write(data)
            sys.stderr.flush()
        # return the result to the client
        self.socket_data_write[connection] = data.encode('UTF-8')
