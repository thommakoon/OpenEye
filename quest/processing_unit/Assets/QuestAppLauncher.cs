using UnityEngine;

/// <summary>
/// Launch another installed Quest/Android app by package name, then quit this one.
/// Tries several Intent strategies — getLaunchIntentForPackage alone fails on some
/// Quest / Android 11+ builds if &lt;queries&gt; is incomplete.
/// </summary>
public static class QuestAppLauncher
{
    const int FlagActivityNewTask = 0x10000000; // Intent.FLAG_ACTIVITY_NEW_TASK
    const int FlagActivityClearTask = 0x00008000; // Intent.FLAG_ACTIVITY_CLEAR_TASK

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
                AndroidJavaObject intent = pm.Call<AndroidJavaObject>("getLaunchIntentForPackage", packageName);

                if (intent == null)
                {
                    // Explicit MAIN + LAUNCHER (works when package is known via <queries>)
                    intent = BuildExplicitLaunchIntent(pm, packageName, "android.intent.category.LAUNCHER");
                }
                if (intent == null)
                {
                    // Meta Quest VR category
                    intent = BuildExplicitLaunchIntent(pm, packageName, "com.oculus.intent.category.VR");
                }

                if (intent == null)
                {
                    Debug.LogError(
                        $"[QuestAppLauncher] No launch intent for '{packageName}'. " +
                        "Is the APK installed? Rebuild OpenEye after updating " +
                        "Assets/Plugins/Android/AndroidManifest.xml <queries>.");
                    return false;
                }

                intent.Call<AndroidJavaObject>("addFlags", FlagActivityNewTask | FlagActivityClearTask);
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

#if UNITY_ANDROID && !UNITY_EDITOR
    static AndroidJavaObject BuildExplicitLaunchIntent(AndroidJavaObject pm, string packageName, string category)
    {
        try
        {
            using (var intent = new AndroidJavaObject("android.content.Intent", "android.intent.action.MAIN"))
            {
                intent.Call<AndroidJavaObject>("addCategory", category);
                intent.Call<AndroidJavaObject>("setPackage", packageName);
                using (var resolve = pm.Call<AndroidJavaObject>("resolveActivity", intent, 0))
                {
                    if (resolve == null)
                        return null;
                }
                // Need a fresh Intent to return (owned)
                var outIntent = new AndroidJavaObject("android.content.Intent", "android.intent.action.MAIN");
                outIntent.Call<AndroidJavaObject>("addCategory", category);
                outIntent.Call<AndroidJavaObject>("setPackage", packageName);
                return outIntent;
            }
        }
        catch (System.Exception e)
        {
            Debug.LogWarning($"[QuestAppLauncher] explicit intent ({category}) failed: {e.Message}");
            return null;
        }
    }
#endif

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
