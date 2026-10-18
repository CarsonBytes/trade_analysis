# IBKR LIVE API relay on Windows (spec GATEWAY_PASSKEY, 2026-10-14).
#
# WHY THIS EXISTS: the live gateway's jts.ini has TrustedIPs=127.0.0.1, so the gateway
# accepts a TCP connection from a remote IP (WSL/container) but closes it the moment any
# data arrives. Every API handshake from WSL or a Docker container therefore got EOF while
# localhost worked. This user-mode relay listens on 0.0.0.0:4003 and forwards to
# 127.0.0.1:4001, so the gateway always sees a localhost connection (satisfying TrustedIPs)
# while WSL/containers reach it at Windows:4003.
#
# Chain: dashboard container -> host.docker.internal:4003 (win-gateway-relay socat)
#        -> Windows:4003 (this relay) -> 127.0.0.1:4001 (IBKR Gateway API).
#
# Registered as scheduled task \IBKR\API Relay by setup-api-relay.ps1 (runs at logon).
# Log: C:\IBC-Live\Logs\relay.log
$ErrorActionPreference = 'Stop'
$Log = 'C:\IBC-Live\Logs\relay.log'
function Log($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $Log -Encoding UTF8 }

Add-Type @"
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;

public class TcpRelay {
    public static void Pump(TcpClient from, TcpClient to) {
        byte[] buf = new byte[65536];
        NetworkStream fs = null, ts = null;
        try {
            fs = from.GetStream();
            ts = to.GetStream();
            int n;
            while ((n = fs.Read(buf, 0, buf.Length)) > 0) {
                ts.Write(buf, 0, n);
                ts.Flush();
            }
        } catch { }
        try { to.Client.Shutdown(SocketShutdown.Send); } catch { }
    }

    public static void Handle(TcpClient client, string remote, string logPath) {
        TcpClient upstream = null;
        try {
            upstream = new TcpClient();
            upstream.Connect("127.0.0.1", 4001);
            System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + remote + " -> upstream connected\n");
            var t1 = new Thread(() => Pump(client, upstream)); t1.IsBackground = true; t1.Start();
            var t2 = new Thread(() => Pump(upstream, client)); t2.IsBackground = true; t2.Start();
            t1.Join(300000); t2.Join(300000);
            System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + remote + " -> pump done\n");
        } catch (Exception e) {
            System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + remote + " -> error: " + e.Message + "\n");
        } finally {
            try { client.Close(); } catch {}
            try { if (upstream != null) upstream.Close(); } catch {}
            System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + remote + " -> closed\n");
        }
    }

    public static void Run(int listenPort, int targetPort, string logPath) {
        var listener = new TcpListener(IPAddress.Any, listenPort);
        listener.Start();
        System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " listening on 0.0.0.0:" + listenPort + " -> 127.0.0.1:" + targetPort + "\n");
        // Resilient accept loop (2026-10-17): the previous bare loop let ANY thrown
        // socket error (e.g. a network change) kill the whole relay, and it stayed
        // dead unnoticed from 2026-10-16 10:26 until 2026-10-17. A failed Accept now
        // logs, backs off, and keeps the listener alive.
        while (true) {
            TcpClient client = null;
            try {
                client = listener.AcceptTcpClient();
            } catch (Exception e) {
                System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " ACCEPT ERROR: " + e.Message + " -- retrying in 3s\n");
                try { listener.Stop(); listener.Start(); } catch { }
                System.Threading.Thread.Sleep(3000);
                continue;
            }
            var remote = ((IPEndPoint)client.Client.RemoteEndPoint).ToString();
            System.IO.File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " accept from " + remote + "\n");
            var t = new Thread(() => Handle(client, remote, logPath)); t.IsBackground = true; t.Start();
        }
    }
}
"@

Log "=== relay starting ==="
[TcpRelay]::Run(4003, 4001, $Log)
