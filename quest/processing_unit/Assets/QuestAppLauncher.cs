using UnityEngine;

/// <summary>
/// Launch another installed Quest/Android app by package name, then quit this one.
/// </summary>
public static class QuestAppLauncher
{
    const int FlagActivityNewTask = 0x10000000;

    public static bool TryLaunch(string packageName)
    {
        if (string.IsNullOrEmpty(packageName))
        {
            Debug.LogError("[QuestAppLauncher] Package name is empty.");
            return false;
        }

#if UNITY_ANDROID && !UNITY_EDITOR
        try
        {
            using (var unityPlayer = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
            using (var activity = unityPlayer.GetStatic<AndroidJavaObject>("currentActivity"))
            using (var pm = activity.Call<AndroidJavaObject>("getPackageManager"))
            {
                var intent = pm.Call<AndroidJavaObject>("getLaunchIntentForPackage", packageName);
                if (intent == null)
                {
                    Debug.LogError(
                        $"[QuestAppLauncher] No launch intent for '{packageName}'. Is the APK installed?");
                    return false;
                }

                intent.Call<AndroidJavaObject>("addFlags", FlagActivityNewTask);
                activity.Call("startActivity", intent);
                Debug.Log($"[QuestAppLauncher] Launched {packageName}");
                return true;
            }
        }
        catch (System.Exception e)
        {
            Debug.LogError($"[QuestAppLauncher] Launch failed: {e.Message}");
            return false;
        }
#else
        Debug.Log($"[QuestAppLauncher] Would launch {packageName} (Editor / non-Android).");
        return true;
#endif
    }

    public static void QuitCurrentApp()
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        try
        {
            using (var unityPlayer = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
            using (var activity = unityPlayer.GetStatic<AndroidJavaObject>("currentActivity"))
            {
                activity.Call("finish");
            }
        }
        catch (System.Exception e)
        {
            Debug.LogWarning($"[QuestAppLauncher] activity.finish failed: {e.Message}");
        }
#endif
        Application.Quit();
    }
}
