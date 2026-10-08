using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

internal static class NeedleLauncher
{
    private const uint ErrorIcon = 0x10;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string text, string caption, uint type);

    [STAThread]
    private static int Main()
    {
        try
        {
            string appRoot = AppDomain.CurrentDomain.BaseDirectory;
            string tray = Path.Combine(appRoot, "needle-tray.ps1");
            if (!File.Exists(tray))
            {
                return Fail("The Needle tray script was not found:\n" + tray);
            }

            string powerShell = FindPowerShell();
            if (powerShell == null)
            {
                return Fail("PowerShell was not found.");
            }

            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = powerShell;
            startInfo.Arguments = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File \"" + tray + "\"";
            startInfo.WorkingDirectory = appRoot;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.WindowStyle = ProcessWindowStyle.Hidden;

            using (Process process = Process.Start(startInfo))
            {
                if (process == null)
                {
                    return Fail("PowerShell could not be started.");
                }

                if (process.WaitForExit(750) && process.ExitCode != 0)
                {
                    return Fail("The Needle tray app exited during startup (code " + process.ExitCode + ").");
                }
            }

            return 0;
        }
        catch (Exception error)
        {
            return Fail("Needle could not start.\n\n" + error.Message);
        }
    }

    private static string FindPowerShell()
    {
        string powerShell = FindOnPath("pwsh.exe");
        if (powerShell != null)
        {
            return powerShell;
        }

        string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        powerShell = Path.Combine(windows, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
        if (File.Exists(powerShell))
        {
            return powerShell;
        }

        return FindOnPath("powershell.exe");
    }

    private static string FindOnPath(string fileName)
    {
        string path = Environment.GetEnvironmentVariable("PATH");
        if (String.IsNullOrEmpty(path))
        {
            return null;
        }

        foreach (string entry in path.Split(Path.PathSeparator))
        {
            string directory = Environment.ExpandEnvironmentVariables(entry.Trim().Trim('"'));
            if (directory.Length == 0)
            {
                continue;
            }

            try
            {
                string candidate = Path.Combine(directory, fileName);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (ArgumentException)
            {
                // Ignore malformed PATH entries and continue to Windows PowerShell.
            }
        }

        return null;
    }

    private static int Fail(string message)
    {
        MessageBox(IntPtr.Zero, message, "Needle", ErrorIcon);
        return 1;
    }
}
