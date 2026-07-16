using System.Collections;
using UnityEngine;

/// <summary>
/// OpenEye side: on PC "launchApp", start the target package then quit OpenEye.
/// Official witlab OpenEye has no handoff — this is for gazeGait Practice/MainStudy.
/// </summary>
public class PracticeTaskHandoff : MonoBehaviour
{
    [Header("Fallback package if PC sends empty package")]
    [SerializeField] string practiceTaskPackageName = "com.PracticeMG.MRstressPRACTICE";

    [Header("Sources")]
    public TCPClient tcp;

    [Header("Behavior")]
    [Tooltip("Wait after startActivity before quitting so Quest can switch apps.")]
    [SerializeField] float quitAfterLaunchSec = 0.8f;

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

    void HandleLaunchApp(string packageFromPc)
    {
        string package = string.IsNullOrEmpty(packageFromPc) ? practiceTaskPackageName : packageFromPc;
        Debug.Log($"[PracticeTaskHandoff] launchApp received → {package}");
        LaunchPracticeTask(package);
    }

    public void LaunchNow()
    {
        LaunchPracticeTask(practiceTaskPackageName);
    }

    void LaunchPracticeTask(string package)
    {
        if (_launching)
        {
            Debug.LogWarning("[PracticeTaskHandoff] already launching — ignore");
            return;
        }
        _launching = true;
        StartCoroutine(LaunchRoutine(package));
    }

    IEnumerator LaunchRoutine(string package)
    {
        Debug.Log($"[PracticeTaskHandoff] Handoff to {package}");

        // Launch FIRST while this Activity is still fully alive (Quest is picky).
        if (!QuestAppLauncher.TryLaunch(package))
        {
            Debug.LogError(
                $"[PracticeTaskHandoff] Could not launch '{package}'. " +
                "Install the target APK and rebuild OpenEye with AndroidManifest <queries>.");
            _launching = false;
            yield break;
        }

        yield return new WaitForSecondsRealtime(quitAfterLaunchSec);

        if (tcp != null)
            tcp.Disconnect();

        yield return null;
        QuestAppLauncher.QuitCurrentApp();
    }
}
