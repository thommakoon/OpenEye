using UnityEngine;

/// <summary>
/// Practice-style gaze ray for OpenEye calib: TCP gazeVisual → CenterEye local plane.
/// Draws independently of the shared calib Dot so live gaze feedback is visible.
/// </summary>
[RequireComponent(typeof(LineRenderer))]
public class OpenEyeMappedGazeRay : MonoBehaviour
{
    public TCPClient tcp;
    public CalibRunner calibRunner;
    [Tooltip("Only draw after calib end (same gate as CalibRunner requireCalibEndForGaze).")]
    public bool requireCalibEnd = false;
    public float planeDistanceM = 1f;
    public float rayWidth = 0.004f;
    public Color rayColor = new Color(0f, 0.85f, 1f, 0.9f);

    LineRenderer _line;
    long _lastSeq;
    bool _evalMode;
    Transform _eyeTf;
    float _nextEyeResolveTime;

    void Awake()
    {
        if (tcp == null) tcp = FindObjectOfType<TCPClient>();
        if (calibRunner == null) calibRunner = FindObjectOfType<CalibRunner>();
        _line = GetComponent<LineRenderer>();
        _line.positionCount = 2;
        _line.useWorldSpace = true;
        _line.startWidth = rayWidth;
        _line.endWidth = rayWidth * 0.25f;
        _line.startColor = rayColor;
        _line.endColor = rayColor;
        _line.enabled = false;
        if (_line.sharedMaterial == null)
        {
            var mat = new Material(Shader.Find("Sprites/Default"));
            mat.color = rayColor;
            _line.material = mat;
        }
    }

    void OnEnable()
    {
        if (tcp == null) tcp = FindObjectOfType<TCPClient>();
        if (tcp != null)
        {
            tcp.OnCalibrationEnd += OnCalibEnd;
            tcp.OnResetCalibration += OnCalibReset;
        }
    }

    void OnDisable()
    {
        if (tcp != null)
        {
            tcp.OnCalibrationEnd -= OnCalibEnd;
            tcp.OnResetCalibration -= OnCalibReset;
        }
    }

    void OnCalibEnd() => _evalMode = true;
    void OnCalibReset() { _evalMode = false; _line.enabled = false; }

    void LateUpdate()
    {
        if (requireCalibEnd && !_evalMode)
        {
            _line.enabled = false;
            return;
        }
        if (tcp == null || !tcp.TryGetLatestGaze(ref _lastSeq, out var gaze))
        {
            _line.enabled = false;
            return;
        }

        var refTf = ResolveVrEyeTransform();
        if (refTf == null)
        {
            _line.enabled = false;
            return;
        }

        Vector3 local = new Vector3(gaze.p.x, gaze.p.y, planeDistanceM);
        Vector3 world = refTf.TransformPoint(local);
        _line.enabled = true;
        _line.SetPosition(0, refTf.position);
        _line.SetPosition(1, world);
    }

    Transform ResolveVrEyeTransform()
    {
        // Cache — GameObject.Find every frame stalls Quest main thread and feeds TCP lag.
        if (_eyeTf != null && _eyeTf.gameObject.activeInHierarchy)
            return _eyeTf;

        float now = Time.unscaledTime;
        if (now < _nextEyeResolveTime && _eyeTf != null)
            return _eyeTf;
        _nextEyeResolveTime = now + 0.5f;

        var centerEye = GameObject.Find("CenterEyeAnchor");
        if (centerEye != null)
        {
            _eyeTf = centerEye.transform;
            return _eyeTf;
        }
        if (Camera.main != null)
        {
            _eyeTf = Camera.main.transform;
            return _eyeTf;
        }
        foreach (var cam in Camera.allCameras)
        {
            if (cam != null && cam.enabled && cam.gameObject.activeInHierarchy)
            {
                _eyeTf = cam.transform;
                return _eyeTf;
            }
        }
        return null;
    }
}
