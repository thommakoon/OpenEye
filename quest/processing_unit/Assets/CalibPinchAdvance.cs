using UnityEngine;
using UnityEngine.XR;
using MixedReality.Toolkit;

/// <summary>
/// Pinch (right or left hand) to advance the calibration step.
/// Sends a "nextStep" message to the PC, which advances exactly like pressing Space.
/// Add to the Networking object; no other wiring needed (uses MRTK HandsAggregator).
/// </summary>
public class CalibPinchAdvance : MonoBehaviour
{
    [Header("Sources")]
    public TCPClient tcp;

    [Header("Pinch")]
    [Tooltip("Pinch amount (0..1) needed to count as a pinch.")]
    [Range(0.5f, 1f)] public float pinchThreshold = 0.9f;
    [Tooltip("Minimum seconds between two accepted pinches (debounce).")]
    public float cooldownSec = 0.6f;

    [Tooltip("Allow the left hand to advance too.")]
    public bool allowLeftHand = true;

    bool _rightWasPinching;
    bool _leftWasPinching;
    float _lastSendTime = -999f;

    void Awake()
    {
        if (tcp == null)
            tcp = FindObjectOfType<TCPClient>();
    }

    void Update()
    {
        if (tcp == null)
            return;

        bool rightRising = CheckHandRising(XRNode.RightHand, ref _rightWasPinching);
        bool leftRising = allowLeftHand && CheckHandRising(XRNode.LeftHand, ref _leftWasPinching);

        if (rightRising || leftRising)
            TrySendNextStep();
    }

    bool CheckHandRising(XRNode hand, ref bool wasPinching)
    {
        var agg = XRSubsystemHelpers.HandsAggregator;
        if (agg == null)
        {
            wasPinching = false;
            return false;
        }

        if (!agg.TryGetPinchProgress(hand, out bool ready, out bool _, out float amount) || !ready)
        {
            wasPinching = false;
            return false;
        }

        bool pinchingNow = amount >= pinchThreshold;
        bool rising = pinchingNow && !wasPinching;
        wasPinching = pinchingNow;
        return rising;
    }

    void TrySendNextStep()
    {
        if (Time.unscaledTime - _lastSendTime < cooldownSec)
            return;

        if (tcp.SendControl("nextStep"))
        {
            _lastSendTime = Time.unscaledTime;
            Debug.Log("[CalibPinchAdvance] Sent nextStep to PC");
        }
    }
}
