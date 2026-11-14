// GUI-only front end to the unmodified portable launcher. No application logic lives here.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("Syllafolio")]
[assembly: AssemblyDescription("Syllafolio Windows desktop launcher")]
[assembly: AssemblyProduct("Syllafolio")]
[assembly: AssemblyVersion("1.0.2.0")]
[assembly: AssemblyFileVersion("1.0.2.0")]
[assembly: AssemblyInformationalVersion("1.0.2")]

internal static class SyllafolioLauncher
{
    private static readonly object OutputLock = new object();
    private static readonly StringBuilder Tail = new StringBuilder();
    private static StreamWriter Log;
    private static string LogError;

    private static string SystemError(Exception exception)
    {
        string text = "Error type: " + exception.GetType().Name + "; system code: 0x" + exception.HResult.ToString("X8", CultureInfo.InvariantCulture) + ".";
        Win32Exception native = exception as Win32Exception;
        if (native != null) text += " Win32 code: " + native.NativeErrorCode.ToString(CultureInfo.InvariantCulture) + ".";
        return text;
    }

    private static void ShowError(string message)
    {
        // Win32 MessageBox uses the Windows display language for its buttons.
        // An owned form keeps every launcher control explicitly English.
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (Form dialog = new Form())
        {
            dialog.Text = "Syllafolio startup";
            dialog.StartPosition = FormStartPosition.CenterScreen;
            dialog.AutoScaleMode = AutoScaleMode.Dpi;
            dialog.Font = new Font("Segoe UI", 10);
            dialog.ClientSize = new Size(620, 330);
            dialog.MinimumSize = new Size(480, 280);
            dialog.MinimizeBox = false;
            dialog.MaximizeBox = false;
            try { dialog.Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }
            TextBox detail = new TextBox();
            detail.Multiline = true; detail.ReadOnly = true; detail.WordWrap = true;
            detail.ScrollBars = ScrollBars.Vertical; detail.Text = message;
            detail.Location = new Point(20, 20); detail.Size = new Size(580, 245);
            detail.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            detail.BackColor = SystemColors.Window; detail.TabStop = false;
            Button ok = new Button(); ok.Text = "OK"; ok.DialogResult = DialogResult.OK;
            ok.Location = new Point(500, 282); ok.Size = new Size(100, 32);
            ok.Anchor = AnchorStyles.Right | AnchorStyles.Bottom;
            dialog.Controls.Add(detail); dialog.Controls.Add(ok);
            dialog.AcceptButton = ok; dialog.CancelButton = ok;
            dialog.Shown += delegate { ok.Focus(); };
            dialog.ShowDialog();
        }
    }

    // Windows CRT command-line rules: every argument is separately quoted, including
    // empty strings; backslashes before a quote or the final quote must be doubled.
    internal static string QuoteArgument(string argument)
    {
        if (argument == null) throw new ArgumentNullException("argument");
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char value in argument)
        {
            if (value == '\\') { slashes++; continue; }
            if (value == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
            }
            else
            {
                result.Append('\\', slashes);
                result.Append(value);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    private static string Json(string value)
    {
        if (value == null) return "null";
        StringBuilder result = new StringBuilder("\"");
        foreach (char item in value)
        {
            switch (item)
            {
                case '"': result.Append("\\\""); break;
                case '\\': result.Append("\\\\"); break;
                case '\r': result.Append("\\r"); break;
                case '\n': result.Append("\\n"); break;
                case '\t': result.Append("\\t"); break;
                default:
                    if (item < 32) result.Append("\\u" + ((int)item).ToString("x4"));
                    else result.Append(item);
                    break;
            }
        }
        return result.Append('"').ToString();
    }

    private static void ReceiveLine(object sender, DataReceivedEventArgs eventArgs)
    {
        if (eventArgs.Data == null) return;
        lock (OutputLock)
        {
            Tail.AppendLine(eventArgs.Data);
            if (Tail.Length > 16000) Tail.Remove(0, Tail.Length - 12000);
            try { Log.WriteLine(eventArgs.Data); Log.Flush(); }
            catch (Exception exception) { LogError = SystemError(exception); }
        }
    }

    [STAThread]
    private static int Main(string[] arguments)
    {
        System.Threading.Thread.CurrentThread.CurrentUICulture = CultureInfo.GetCultureInfo("en-US");
        string root = Path.GetDirectoryName(Path.GetFullPath(Assembly.GetExecutingAssembly().Location));
        string reportDirectory = null;
        string error = null;
        int exitCode = 1;
        int childId = 0;
        try
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (String.IsNullOrEmpty(local)) throw new IOException("The per-user application-data directory is unavailable.");
            reportDirectory = Path.Combine(local, "Syllafolio", "launcher", DateTime.UtcNow.ToString("yyyyMMddTHHmmss") + "_" + Guid.NewGuid().ToString("N").Substring(0, 12));
            Directory.CreateDirectory(reportDirectory);
            Log = new StreamWriter(new FileStream(Path.Combine(reportDirectory, "launcher.log"), FileMode.CreateNew, FileAccess.Write, FileShare.Read), new UTF8Encoding(false));
            string python = Path.Combine(root, "_runtime", "python.exe");
            string launcher = Path.Combine(root, "run_launcher.py");
            foreach (string file in new string[] { python, launcher, Path.Combine(root, "launch.py"), Path.Combine(root, "installation_lease.py") })
                if (!File.Exists(file)) throw new FileNotFoundException("This installation is incomplete. Reinstall Syllafolio into a new directory; existing projects will not be changed.", file);

            List<string> childArguments = new List<string>();
            childArguments.Add("-B"); childArguments.Add("-X"); childArguments.Add("utf8"); childArguments.Add(launcher);
            childArguments.AddRange(arguments);
            StringBuilder commandLine = new StringBuilder();
            foreach (string argument in childArguments)
            {
                if (commandLine.Length > 0) commandLine.Append(' ');
                commandLine.Append(QuoteArgument(argument));
            }
            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = python;
            start.Arguments = commandLine.ToString();
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.RedirectStandardInput = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            Log.WriteLine("Syllafolio GUI launcher. Application entry: " + launcher);
            Log.Flush();
            using (Process process = new Process())
            {
                process.StartInfo = start;
                process.OutputDataReceived += ReceiveLine;
                process.ErrorDataReceived += ReceiveLine;
                if (!process.Start()) throw new IOException("The bundled Python process did not start.");
                childId = process.Id;
                process.StandardInput.Close();
                process.BeginOutputReadLine(); process.BeginErrorReadLine();
                process.WaitForExit();
                // The parameterless overload also drains asynchronous stdout/stderr.
                exitCode = process.ExitCode;
            }
            if (exitCode != 0)
            {
                error = "Syllafolio could not start or closed with an error (exit code " + exitCode + ").";
                MatchCollection codes = Regex.Matches(Tail.ToString(), @"(?<![A-Z0-9_])([A-Z][A-Z0-9_]{2,}):");
                if (codes.Count > 0)
                {
                    string code = codes[codes.Count - 1].Groups[1].Value;
                    error += "\nReported error code: " + code + ".";
                    if (code == "INSTALLATION_BUSY") error += "\nThis installation is running or being updated. Close its windows and checks, then retry.";
                    if (code == "INSTALLATION_RECOVERY_REQUIRED") error += "\nAn installation change did not finish. Keep its backup and use a verified independent installation or restore this one before retrying.";
                }
            }
        }
        catch (Exception exception)
        {
            error = (exception is FileNotFoundException ? "This installation is incomplete. Reinstall Syllafolio into a new directory; existing projects will not be changed.\n" : "The launcher could not complete the application start.\n") + SystemError(exception);
            exitCode = 1;
            if (Log != null)
            {
                try { Log.WriteLine(exception.ToString()); Log.Flush(); }
                catch (Exception logException) { LogError = SystemError(logException); }
            }
        }
        finally
        {
            if (Log != null)
            {
                try { Log.Dispose(); }
                catch (Exception logException) { LogError = SystemError(logException); }
                Log = null;
            }
            if (LogError != null)
            {
                error = (error == null ? "" : error + "\n\n") + "The launcher log could not be saved: " + LogError;
                if (exitCode == 0) exitCode = 1;
            }
            if (reportDirectory != null)
            {
                try
                {
                    string receipt = "{\n  \"schema\": \"syllafolio-gui-launch-v1\",\n  \"root\": " + Json(root) +
                        ",\n  \"child_pid\": " + childId + ",\n  \"exit_code\": " + exitCode +
                        ",\n  \"error\": " + Json(error) + ",\n  \"application_entry\": \"run_launcher.py\",\n  \"project_actions_performed_by_launcher\": false\n}\n";
                    File.WriteAllText(Path.Combine(reportDirectory, "launcher_result.json"), receipt, new UTF8Encoding(false));
                }
                catch (Exception receiptError)
                {
                    error = (error == null ? "" : error + "\n\n") + "The launcher result could not be saved. " + SystemError(receiptError);
                    if (exitCode == 0) exitCode = 1;
                }
            }
        }
        if (error != null)
        {
            string logNote = reportDirectory == null ? "A launcher log could not be created." : "Launcher log:\n" + Path.Combine(reportDirectory, "launcher.log");
            ShowError(error + "\n\n" + logNote + "\n\nThe log contains the exact application output and original failure details. No project data was changed by this launcher.");
        }
        return exitCode;
    }
}
