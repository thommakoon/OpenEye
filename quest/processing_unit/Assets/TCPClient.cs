using System;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using System.Text;
using UnityEngine;
using UnityEngine.SceneManagement;

[Serializable] public class PayloadStep { public int step; }
[Serializable] public class Pos { public float x; public float y; } // meters on z=1m plane
[Serializable] public class PayloadEval { public int idx; public int t_ms; public Pos pos; }
[Serializable] public class PayloadGaze { public double t; public float x; public float y; }

[Serializable] public class PayloadLaunch { public string package; }
[Serializable] public class PayloadTimeEcho { public long pc_t1_ms; public long quest_tH_ms; }

[Serializable] class MsgTypeOnly { public string type; }
[Serializable] class MsgUpdateStep { public string type; public PayloadStep payload; }
[Serializable] class MsgEvalTarget { public string type; public PayloadEval payload; }
[Serializable] class MsgGaze { public string type; public PayloadGaze payload; }
[Serializable] class MsgLaunch { public string type; public PayloadLaunch payload; }
[Serializable] class MsgTimeEcho { public string type; public PayloadTimeEcho payload; }

public class TCPClient : MonoBehaviour
{
    public static TCPClient Instance;

    [Header("Server")]
    public string serverIp = "192.168.0.50"; // Enter your IP address
    public int serverPort = 5051;

    [Header("Auto Connect / Reconnect")]
    [SerializeField] bool autoConnectOnStart = true;
    [SerializeField] bool autoReconnect = true;
    [SerializeField, Tooltip("Reconnect period")] float reconnectIntervalSec = 2.0f;

    [Header("Logging")]
    [SerializeField, Tooltip("Console Logging Period")] int logEveryN = 10;

    public enum State { Disconnected, Connecting, Connected, Failed }
    public State CurrentState { get; private set; } = State.Disconnected;

    TcpClient _client;
    NetworkStream _stream;
    Thread _recvThread;
    CancellationTokenSource _cts;

    public event Action<int> OnUpdateStep;                 // step
    public event Action OnCalibrationEnd;                  // calib end
    public event Action OnResetCalibration;
    public event Action<int,int,Vector2> OnEvalTarget;     // (idx, t_ms, (x_m, y_m))
    public event Action<double,Vector2> OnGazeVisual;
    public event Action<State> OnStateChanged;
    public event Action<string> OnLaunchApp;               // package name (may be empty)

    volatile int _pendingStep = -1;
    volatile bool _pendingCalibEnd = false;
    volatile bool _pendingResetCalib = false;
    volatile bool _pendingLaunchApp = false;
    volatile string _pendingLaunchPackage = "";

    struct EvalMsg { public int idx, tms; public Vector2 p_m; }
    volatile bool _hasLatestEval = false;
    EvalMsg _latestEval;

    public struct GazeMsg { public double t; public Vector2 p; }

    public string nextSceneName = "PL_Calibration_OpenCV";
    [SerializeField] bool loadSceneOnCalibEnd = false;

    public volatile float latestGazeX;
    public volatile float latestGazeY;
    public volatile float GazeTimestamp;

    const int GAZE_BUF_CAP = 10;
    readonly object _gazeLock = new object();
    GazeMsg[] _gazeBuf = new GazeMsg[GAZE_BUF_CAP];
    int _gazeHead = 0;
    long _gazeSeq = 0;

    int _logCounter;

    void Awake()
    {   
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }

        Instance = this;
        DontDestroyOnLoad(gameObject);

        // Not in official OpenEye; must be present so PC Start Practice / Main Study works.
        if (GetComponent<PracticeTaskHandoff>() == null)
            gameObject.AddComponent<PracticeTaskHandoff>();

        if (autoConnectOnStart)
            _ = StartConnectLoop();
    }
    void OnDestroy()
    {
        CloseConnectionSafely();
    }

    private void CloseConnectionSafely()
    {
        try {
            _recvThread?.Interrupt();
            _recvThread?.Join(100);
        } catch {}

        try { _stream?.Close(); } catch {}
        try { _client?.Close(); } catch {}
    }
    async Task StartConnectLoop()
    {
        while (autoReconnect && Application.isPlaying)
        {
            if (CurrentState == State.Disconnected || CurrentState == State.Failed)
                Connect();

            try { await Task.Delay(TimeSpan.FromSeconds(reconnectIntervalSec)); }
            catch {}
        }
    }

    public void Connect()
    {
        if (CurrentState == State.Connecting || CurrentState == State.Connected) return;
        _cts?.Cancel();
        _cts = new CancellationTokenSource();
        _ = ConnectAsync(_cts.Token);
    }

    public void Disconnect()
    {
        try { _cts?.Cancel(); _stream?.Close(); _client?.Close(); } catch { }
        SetState(State.Disconnected);
    }

    async Task ConnectAsync(CancellationToken token)
    {
        SetState(State.Connecting);
        try
        {
            // Keep socket buffers small so stare gazeVisual can't sit as a multi-second FIFO.
            // Practice OpenEyeGazeReceiver uses default buffers + a dedicated sync thread.
            _client = new TcpClient
            {
                NoDelay = true,
                ReceiveBufferSize = 64 * 1024,
                SendBufferSize    = 64 * 1024
            };

            var connectTask = _client.ConnectAsync(serverIp, serverPort);
            using (token.Register(() => { try { _client?.Close(); } catch { } }))
            {
                await connectTask;
            }

            if (!_client.Connected) throw new Exception("connect failed");

            _stream = _client.GetStream();
            SetState(State.Connected);

            // Dedicated thread (not async Task): keeps up with 60–100 Hz gaze without
            // Unity await scheduling delays that let TCP backlog grow past 1s.
            _recvThread = new Thread(() => ReceiveLoop(token))
            {
                IsBackground = true,
                Name = "OpenEyeTCPRecv"
            };
            _recvThread.Start();
        }
        catch (Exception e)
        {
            Debug.LogError($"[TCP] connect error: {e.Message}");
            SetState(State.Failed);
        }
    }

    void ReceiveLoop(CancellationToken token)
    {
        var headerBuf = new byte[4];
        byte[] payloadBuf = new byte[512];

        try
        {
            while (!token.IsCancellationRequested)
            {
                string json = ReadOneFrame(_stream, headerBuf, ref payloadBuf);
                GazeMsg? latestGaze = null;
                HandleFrame(json, ref latestGaze);

                // Drain already-buffered frames without waiting: for gazeVisual keep only
                // the newest sample so a temporary stall cannot become multi-second lag.
                while (TryPeekCompleteFrameLength(out int nextLen) &&
                       _client != null &&
                       _client.Available >= 4 + nextLen)
                {
                    json = ReadOneFrame(_stream, headerBuf, ref payloadBuf);
                    HandleFrame(json, ref latestGaze);
                }

                if (latestGaze.HasValue)
                    ApplyGaze(latestGaze.Value);
            }
        }
        catch (Exception e)
        {
            if (!token.IsCancellationRequested)
                Debug.LogWarning($"[TCP] recv loop end: {e.Message}");
        }

        Disconnect();
    }

    bool TryPeekCompleteFrameLength(out int len)
    {
        len = 0;
        try
        {
            if (_client == null || !_client.Connected || _client.Available < 4)
                return false;

            var peek = new byte[4];
            int n = _client.Client.Receive(peek, 0, 4, SocketFlags.Peek);
            if (n < 4)
                return false;

            len = (peek[0] << 24) | (peek[1] << 16) | (peek[2] << 8) | peek[3];
            return len > 0 && len <= 10_000_000;
        }
        catch
        {
            return false;
        }
    }

    static string ReadOneFrame(NetworkStream stream, byte[] headerBuf, ref byte[] payloadBuf)
    {
        ReadExact(stream, headerBuf, 0, 4);
        int len = (headerBuf[0] << 24) | (headerBuf[1] << 16) | (headerBuf[2] << 8) | headerBuf[3];
        if (len <= 0 || len > 10_000_000)
            throw new Exception($"invalid length: {len}");

        if (payloadBuf.Length < len)
            payloadBuf = new byte[len];

        ReadExact(stream, payloadBuf, 0, len);
        return Encoding.UTF8.GetString(payloadBuf, 0, len);
    }

    void HandleFrame(string json, ref GazeMsg? latestGaze)
    {
        try
        {
            // Fast path: high-rate stream — avoid double JsonUtility when type is obvious.
            if (json.IndexOf("\"gazeVisual\"", StringComparison.Ordinal) >= 0)
            {
                var msg = JsonUtility.FromJson<MsgGaze>(json);
                if (msg?.payload != null)
                {
                    latestGaze = new GazeMsg
                    {
                        t = msg.payload.t,
                        p = new Vector2(msg.payload.x, msg.payload.y)
                    };
                }
                return;
            }

            var head = JsonUtility.FromJson<MsgTypeOnly>(json);
            if (head == null || string.IsNullOrEmpty(head.type))
            {
                if ((_logCounter++ % logEveryN) == 0)
                    Debug.LogWarning($"[TCP] unknown json: {json}");
                return;
            }

            switch (head.type)
            {
                case "updateStep":
                {
                    var msg = JsonUtility.FromJson<MsgUpdateStep>(json);
                    if (msg?.payload != null)
                        _pendingStep = msg.payload.step;
                    break;
                }

                case "calibrationEnd":
                {
                    _pendingCalibEnd = true;
                    if (loadSceneOnCalibEnd && !string.IsNullOrEmpty(nextSceneName))
                        SceneManager.LoadScene(nextSceneName);
                    break;
                }

                case "resetCalib":
                    _pendingResetCalib = true;
                    break;

                case "launchApp":
                {
                    var msg = JsonUtility.FromJson<MsgLaunch>(json);
                    _pendingLaunchPackage = msg?.payload?.package ?? "";
                    _pendingLaunchApp = true;
                    break;
                }

                case "timeEcho":
                {
                    var echo = JsonUtility.FromJson<MsgTimeEcho>(json);
                    long t1 = echo?.payload != null ? echo.payload.pc_t1_ms : 0L;
                    long tH = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                    string reply =
                        "{\"type\":\"timeEcho\",\"payload\":{\"pc_t1_ms\":" + t1 +
                        ",\"quest_tH_ms\":" + tH + "}}";
                    SendRawJson(reply);
                    break;
                }

                case "evalTarget":
                {
                    var msg = JsonUtility.FromJson<MsgEvalTarget>(json);
                    if (msg?.payload != null && msg.payload.pos != null)
                    {
                        _latestEval = new EvalMsg
                        {
                            idx = msg.payload.idx,
                            tms = msg.payload.t_ms,
                            p_m = new Vector2(msg.payload.pos.x, msg.payload.pos.y)
                        };
                        _hasLatestEval = true;
                    }
                    break;
                }

                default:
                    if ((_logCounter++ % logEveryN) == 0)
                        Debug.Log($"[TCP] unknown type: {head.type}");
                    break;
            }
        }
        catch (Exception pe)
        {
            Debug.LogWarning($"[TCP] parse error: {pe.Message}\nJSON={json}");
        }
    }

    void ApplyGaze(GazeMsg gaze)
    {
        EnqueueGaze(gaze);
        latestGazeX = gaze.p.x;
        latestGazeY = gaze.p.y;
        GazeTimestamp = (float)gaze.t;
    }

    readonly object _sendLock = new object();

    /// <summary>
    /// Send a simple control message to the PC server, e.g. type="nextStep".
    /// Safe to call from the main thread.
    /// </summary>
    public bool SendControl(string type)
    {
        return SendRawJson("{\"type\":\"" + type + "\",\"payload\":{}}");
    }

    bool SendRawJson(string json)
    {
        var stream = _stream;
        if (stream == null || CurrentState != State.Connected)
            return false;

        try
        {
            byte[] payload = Encoding.UTF8.GetBytes(json);
            int len = payload.Length;
            byte[] header = new byte[4]
            {
                (byte)((len >> 24) & 0xFF),
                (byte)((len >> 16) & 0xFF),
                (byte)((len >> 8) & 0xFF),
                (byte)(len & 0xFF),
            };
            lock (_sendLock)
            {
                stream.Write(header, 0, 4);
                stream.Write(payload, 0, payload.Length);
                stream.Flush();
            }
            return true;
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[TCP] send failed: {e.Message}");
            return false;
        }
    }

    static void ReadExact(NetworkStream s, byte[] buf, int off, int len)
    {
        int got = 0;
        while (got < len)
        {
            int r = s.Read(buf, off + got, len - got);
            if (r <= 0) throw new Exception("socket closed");
            got += r;
        }
    }

    void SetState(State s)
    {
        CurrentState = s;
        Debug.Log($"[TCP] state = {s}");
        OnStateChanged?.Invoke(s);
        if (s == State.Connected)
            SendSessionHello("connected");
    }

    void SendSessionHello(string scene)
    {
        string pkg = Application.identifier;
        string safeScene = string.IsNullOrEmpty(scene) ? "connected" : scene;
        string json =
            "{\"type\":\"sessionHello\",\"payload\":{\"package\":\"" + pkg +
            "\",\"scene\":\"" + safeScene + "\"}}";
        SendRawJson(json);
        Debug.Log($"[TCP] sessionHello package={pkg} scene={safeScene}");
    }

    void OnApplicationQuit() => Disconnect();

    void EnqueueGaze(GazeMsg m)
    {
        lock (_gazeLock)
        {
            _gazeBuf[_gazeHead] = m;
            _gazeHead = (_gazeHead + 1) % GAZE_BUF_CAP;
            _gazeSeq++;
        }
    }

    public bool TryGetLatestGaze(ref long lastSeenSeq, out GazeMsg latest)
    {
        lock (_gazeLock)
        {
            if (_gazeSeq == lastSeenSeq)
            {
                latest = default;
                return false;
            }
            int latestIdx = (_gazeHead - 1 + GAZE_BUF_CAP) % GAZE_BUF_CAP;
            latest = _gazeBuf[latestIdx];
            lastSeenSeq = _gazeSeq;
            return true;
        }
    }

    void OnReceiveGazeVisual(float x, float y, float t)
    {
        latestGazeX = x;
        latestGazeY = y;
        GazeTimestamp = t;
    }

    void Update()
    {
        // 1) calibrationEnd
        if (_pendingCalibEnd)
        {
            _pendingCalibEnd = false;
            OnCalibrationEnd?.Invoke();
        }

        // 1b) resetCalib
        if (_pendingResetCalib)
        {
            _pendingResetCalib = false;
            OnResetCalibration?.Invoke();
        }

        // 1c) launchApp (PC-driven handoff)
        if (_pendingLaunchApp)
        {
            _pendingLaunchApp = false;
            OnLaunchApp?.Invoke(_pendingLaunchPackage);
        }

        // 2) updateStep
        if (_pendingStep >= 0)
        {
            int step = _pendingStep;
            _pendingStep = -1;
            OnUpdateStep?.Invoke(step);
        }

        // 3) evalTarget
        if (_hasLatestEval)
        {
            _hasLatestEval = false;
            var m = _latestEval;
            OnEvalTarget?.Invoke(m.idx, m.tms, m.p_m); // meters
        }

    }
}
