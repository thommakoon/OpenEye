using System.Collections;
using UnityEngine;

/// <summary>
/// OpenEye side: on the PC's "launchApp" command (or optional local button),
/// launch PracticeTask and quit OpenEye to free the PC TCP socket.
/// Add to the Networking object in NeonQuestCalib.
/// </summary>
public class PracticeTaskHandoff : MonoBehaviour
{
    [Header("Target app (PracticeTask Player Settings > Android package name)")]
    [SerializeField] string practiceTaskPackageName = "com.PracticeMG.MRstressPRACTICE";

    [Header("Sources")]
    public TCPClient tcp;

    [Header("Behavior")]
    [Tooltip("Delay after disconnecting TCP before launching, so PC releases the socket.")]
    [SerializeField] float disconnectDelaySec = 0.3f;

    bool _launching;

    void Awake()
    {
        if (tcp == null)
            tcp = FindObjectOfType<TCPClient>();
    }

    void OnEnable()
    {
        if (tcp != null)
            tcp.OnLaunchApp += HandleLaunchApp;
    }

    void OnDisable()
    {
        if (tcp != null)
            tcp.OnLaunchApp -= HandleLaunchApp;
    }

    // Called when the PC GUI presses "Start Study" (TCP "launchApp").
    void HandleLaunchApp(string packageFromPc)
    {
        string package = string.IsNullOrEmpty(packageFromPc) ? practiceTaskPackageName : packageFromPc;
        LaunchPracticeTask(package);
    }

    // Optional: hook this to a local Unity button too, if you want a manual fallback.
    public void LaunchNow()
    {
        LaunchPracticeTask(practiceTaskPackageName);
    }

    void LaunchPracticeTask(string package)
    {
        if (_launching)
            return;
        _launching = true;
        StartCoroutine(LaunchRoutine(package));
    }

    IEnumerator LaunchRoutine(string package)
    {
        Debug.Log($"[PracticeTaskHandoff] Handoff to {package}");

        if (tcp != null)
            tcp.Disconnect();

        yield return new WaitForSeconds(disconnectDelaySec);

        if (!QuestAppLauncher.TryLaunch(package))
        {
            Debug.LogError($"[PracticeTaskHandoff] Could not launch {package}. Is PracticeTask installed?");
            _launching = false;
            yield break;
        }

        QuestAppLauncher.QuitCurrentApp();
    }
}
