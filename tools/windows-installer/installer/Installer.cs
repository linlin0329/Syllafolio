using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

[assembly: AssemblyTitle("Syllafolio Setup")]
[assembly: AssemblyProduct("Syllafolio")]
[assembly: AssemblyVersion("0.1.0.1")]
[assembly: AssemblyFileVersion("0.1.0.1")]
[assembly: AssemblyInformationalVersion("0.1.0-lite1")]
namespace SyllafolioInstaller
{
    sealed class Failure : Exception
    {
        internal int Exit;
        internal string Code;
        internal Failure(int exit, string code, string message) : base(message)
        {
            Exit = exit;
            Code = code;
        }
    }

    sealed class Options
    {
        internal bool Qa, Quiet, Uninstall, Cleanup;
        internal string Target, Receipt, FailStage, ExpectedSha;
        internal int WaitPid;
        internal static Options Parse(string[] args)
        {
            var o = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                if (a == "--qa")
                    o.Qa = true;
                else if (a == "--quiet")
                    o.Quiet = true;
                else if (a == "--uninstall")
                    o.Uninstall = true;
                else if (a == "--cleanup-cache")
                    o.Cleanup = true;
                else if ((a == "--target" || a == "--receipt" || a == "--fail-stage" || a == "--expected-sha" || a == "--wait-pid") && i + 1 < args.Length)
                {
                    string v = args[++i];
                    if (a == "--target")
                        o.Target = v;
                    else if (a == "--receipt")
                        o.Receipt = v;
                    else if (a == "--fail-stage")
                        o.FailStage = v;
                    else if (a == "--expected-sha")
                        o.ExpectedSha = v;
                    else
                        o.WaitPid = int.Parse(v, CultureInfo.InvariantCulture);
                }
                else
                    throw new Failure(2, "INVALID_ARGUMENT", "Unknown or incomplete argument: " + a);
            }

            if (o.FailStage != null && (!o.Qa || !(new[]
            {
                "before_publish",
                "after_publish",
                "after_registry",
                "cache_write",
                "cleanup_cache",
                "uninstall_files"
            }

            ).Contains(o.FailStage)))
                throw new Failure(2, "INVALID_ARGUMENT", "Failure injection is available only in the isolated QA identity.");
            if (o.Qa && o.Target == null && !o.Cleanup)
                throw new Failure(2, "QA_TARGET_REQUIRED", "QA requires an explicit new container.");
            if (o.Receipt != null)
            {
                o.Receipt = Path.GetFullPath(o.Receipt);
                if (File.Exists(o.Receipt) || Directory.Exists(o.Receipt))
                    throw new Failure(21, "RECEIPT_EXISTS", "Choose a new receipt file. Existing files will not be overwritten.");
            }

            return o;
        }
    }

    sealed class InstallManifest
    {
        public string schema { get; set; }
        public string product { get; set; }
        public string version { get; set; }
        public bool qa { get; set; }
        public string container { get; set; }
        public string app_root { get; set; }
        public string instance { get; set; }
        public string cache_path { get; set; }
        public string cache_sha256 { get; set; }
        public Dictionary<string, string> files { get; set; }
        public Dictionary<string, string> shortcuts { get; set; }
    }

    sealed class ProductMutex : IDisposable
    {
        Mutex mutex;
        bool held;
        internal ProductMutex(bool qa, bool wait)
        {
            string sid = WindowsIdentity.GetCurrent().User.Value;
            mutex = new Mutex(false, "Local\\SyllafolioInstaller-" + sid + (qa ? "-QA" : "-Product"));
            try
            {
                held = mutex.WaitOne(wait ? 60000 : 0);
            }
            catch (AbandonedMutexException)
            {
                held = true;
            }

            if (!held)
            {
                mutex.Dispose();
                throw new Failure(20, "INSTALLER_BUSY", "Another installation or uninstall is still running for this user.");
            }
        }

        public void Dispose()
        {
            if (held)
            {
                held = false;
                mutex.ReleaseMutex();
            }

            mutex.Dispose();
        }
    }

    sealed class InstallationLock : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        struct Overlapped
        {
            internal UIntPtr Internal, InternalHigh;
            internal uint Offset, OffsetHigh;
            internal IntPtr Event;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool LockFileEx(SafeFileHandle file, uint flags, uint reserved, uint low, uint high, ref Overlapped overlap);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool UnlockFileEx(SafeFileHandle file, uint reserved, uint low, uint high, ref Overlapped overlap);
        SafeFileHandle handle;
        Overlapped ov;
        internal InstallationLock(string root)
        {
            string file = Path.Combine(root, ".syllafolio-installation.lock");
            Engine.NoLinks(file);
            handle = CreateFile(Engine.Long(file), 0xC0000000, 3, IntPtr.Zero, 3, 0x200000, IntPtr.Zero);
            if (handle.IsInvalid)
            {
                handle.Dispose();
                throw new Failure(20, "INSTALLATION_BUSY", "The installation lock is unavailable. Close this installation's windows and checks, then retry.");
            }

            if (!LockFileEx(handle, 3, 0, 1, 0, ref ov))
            {
                handle.Dispose();
                throw new Failure(20, "INSTALLATION_BUSY", "Syllafolio is running or being updated. Close its windows and checks, then retry. No application files were removed.");
            }
        }

        public void Dispose()
        {
            if (handle != null)
            {
                UnlockFileEx(handle, 0, 1, 0, ref ov);
                handle.Dispose();
                handle = null;
            }
        }
    }

    sealed class SegmentStream : Stream
    {
        Stream inner;
        long start, length, position;
        internal SegmentStream(Stream s, long start, long length)
        {
            inner = s;
            this.start = start;
            this.length = length;
            position = 0;
            inner.Position = start;
        }

        public override bool CanRead
        {
            get
            {
                return true;
            }
        }

        public override bool CanSeek
        {
            get
            {
                return true;
            }
        }

        public override bool CanWrite
        {
            get
            {
                return false;
            }
        }

        public override long Length
        {
            get
            {
                return length;
            }
        }

        public override long Position
        {
            get
            {
                return position;
            }

            set
            {
                Seek(value, SeekOrigin.Begin);
            }
        }

        public override int Read(byte[] b, int o, int c)
        {
            c = (int)Math.Min(c, length - position);
            if (c <= 0)
                return 0;
            inner.Position = start + position;
            int n = inner.Read(b, o, c);
            position += n;
            return n;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            long p = origin == SeekOrigin.Begin ? offset : origin == SeekOrigin.Current ? position + offset : length + offset;
            if (p < 0 || p > length)
                throw new IOException("Payload seek is outside its boundary.");
            position = p;
            return p;
        }

        public override void Flush()
        {
        }

        public override void SetLength(long v)
        {
            throw new NotSupportedException();
        }

        public override void Write(byte[] b, int o, int c)
        {
            throw new NotSupportedException();
        }
    }

    static class Engine
    {
        [DllImport("kernel32.dll", EntryPoint = "CreateDirectoryW", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool NativeCreateDirectory(string path, IntPtr securityAttributes);
        static void CreateStageDirectory(string path)
        {
            if (!NativeCreateDirectory(Long(path), IntPtr.Zero))
            {
                int error = Marshal.GetLastWin32Error();
                if (error == 183 || error == 80)
                    throw new Failure(23, "STAGING_EXISTS", "The private staging folder already exists. It was not adopted or changed. Retry with a new installation attempt.");
                throw new Failure(25, "STAGING_CREATE", "Setup could not create a private staging folder. Check folder access and free space. Native error: " + error.ToString(CultureInfo.InvariantCulture) + ".");
            }
        }

        internal const string Product = "Syllafolio-Lite";
        internal const string Marker = ".syllafolio-container.json", Manifest = "INSTALLATION.json", Pending = ".syllafolio-update-pending.json";
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer
        {
            MaxJsonLength = 64 * 1024 * 1024,
            RecursionLimit = 100
        };
        internal static volatile bool Cancelled;
        internal static bool FailCacheWrite;
        internal static Action<int, string> Progress = delegate
        {
        };
        internal static string Exe
        {
            get
            {
                return Assembly.GetExecutingAssembly().Location;
            }
        }

        internal static string Long(string p)
        {
            p = Path.GetFullPath(p);
            return p.StartsWith("\\\\?\\", StringComparison.Ordinal) ? p : p.StartsWith("\\\\", StringComparison.Ordinal) ? "\\\\?\\UNC\\" + p.Substring(2) : "\\\\?\\" + p;
        }

        internal static string Full(string p)
        {
            if (p.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase))
                p = "\\\\" + p.Substring(8);
            else if (p.StartsWith("\\\\?\\", StringComparison.Ordinal))
                p = p.Substring(4);
            string full = Path.GetFullPath(p);
            string root = Path.GetPathRoot(full);
            return full.Length > root.Length ? full.TrimEnd(Path.DirectorySeparatorChar) : full;
        }

        internal static bool Same(string a, string b)
        {
            return string.Equals(Full(a), Full(b), StringComparison.OrdinalIgnoreCase);
        }

        internal static string Hash(byte[] b)
        {
            using (var h = SHA256.Create())
                return Hex(h.ComputeHash(b));
        }

        internal static string HashFile(string p)
        {
            using (var f = new FileStream(Long(p), FileMode.Open, FileAccess.Read, FileShare.Read))
            using (var h = SHA256.Create())
                return Hex(h.ComputeHash(f));
        }

        internal static string UserMessage(Exception error)
        {
            var failure = error as Failure;
            if (failure != null)
                return failure.Message;
            if (error is UnauthorizedAccessException)
                return "Windows denied access to a required file or folder. Choose a writable installation location and close programs using it.";
            if (error is PathTooLongException)
                return "A file path is too long. Choose a shorter installation location.";
            if (error is IOException)
                return "A file operation failed. Check free disk space, folder permissions and running programs. Error: " + error.HResult.ToString("X8", CultureInfo.InvariantCulture) + ".";
            return "Setup could not complete this operation. Error: " + error.GetType().Name + " (" + error.HResult.ToString("X8", CultureInfo.InvariantCulture) + ").";
        }

        static string Hex(byte[] b)
        {
            return BitConverter.ToString(b).Replace("-", "").ToLowerInvariant();
        }

        internal static void NoLinks(string p)
        {
            p = Full(p);
            while (!string.IsNullOrEmpty(p))
            {
                if ((File.Exists(Long(p)) || Directory.Exists(Long(p))) && (File.GetAttributes(Long(p)) & FileAttributes.ReparsePoint) != 0)
                    throw new Failure(21, "UNSAFE_PATH", "Links and reparse points are refused: " + p);
                string parent = Path.GetDirectoryName(p);
                if (parent == p)
                    break;
                p = parent;
            }
        }

        static string Safe(string root, string name)
        {
            if (string.IsNullOrEmpty(name) || name.Contains('\\') || name.StartsWith("/") || name.Split('/').Any(x => x.Length == 0 || x == "." || x == ".." || x.EndsWith(".") || x.EndsWith(" ") || x.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0))
                throw new Failure(22, "PAYLOAD_PATH", "The payload contains an unsafe relative path.");
            string p = Full(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
            if (!p.StartsWith(Full(root) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new Failure(22, "PAYLOAD_PATH", "The payload escaped the installation root.");
            NoLinks(p);
            return p;
        }

        static void CheckCancel()
        {
            if (Cancelled)
                throw new Failure(24, "CANCELLED", "Installation was cancelled; published application files have been rolled back.");
        }

        internal static string Key(bool qa)
        {
            return @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Product + (qa ? "-QA" : "");
        }

        internal static string RootKey(string root)
        {
            return @"Software\Syllafolio\Installer\Roots\" + Hash(Encoding.UTF8.GetBytes(Full(root).ToUpperInvariant())).Substring(0, 32);
        }

        internal static string Group(bool qa)
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), qa ? "Syllafolio Lite QA" : "Syllafolio");
        }

        static string Cache(bool qa)
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Syllafolio", "Installer", qa ? "Syllafolio-QA-Uninstall.exe" : "Syllafolio-Uninstall.exe");
        }

        static void Save(string p, object value)
        {
            Directory.CreateDirectory(Long(Path.GetDirectoryName(p)));
            File.WriteAllText(Long(p), Json.Serialize(value), new UTF8Encoding(false));
        }

        static void FailAt(Options o, string stage)
        {
            if (o.FailStage == stage)
                throw new Failure(25, "QA_INJECTED_FAILURE", "Explicit QA failure at " + stage + ".");
        }

        static string Q(string s)
        {
            return "\"" + s.Replace("\"", "\\\"") + "\"";
        }

        static string Value(RegistryKey k, string n)
        {
            return Convert.ToString(k.GetValue(n), CultureInfo.InvariantCulture);
        }

        static bool RootOwned(string root, bool qa)
        {
            string m = Path.Combine(root, Marker);
            if (!File.Exists(Long(m)))
                return false;
            NoLinks(m);
            using (var k = Registry.CurrentUser.OpenSubKey(RootKey(root)))
            {
                if (k == null)
                    return false;
                return Value(k, "Product") == Product && Value(k, "Qa") == qa.ToString() && Same(Value(k, "Container"), root) && Value(k, "MarkerSha256") == HashFile(m);
            }
        }

        static void RegisterRoot(string root, bool qa)
        {
            Save(Path.Combine(root, Marker), new { schema = "syllafolio-install-container-v1", product = Product, qa = qa, token = Guid.NewGuid().ToString("N") });
            using (var k = Registry.CurrentUser.CreateSubKey(RootKey(root)))
            {
                k.SetValue("Product", Product);
                k.SetValue("Qa", qa.ToString());
                k.SetValue("Container", root);
                k.SetValue("MarkerSha256", HashFile(Path.Combine(root, Marker)));
            }
        }

        static void RequireContainer(string root, Options o)
        {
            if (!Path.IsPathRooted(root) || root.StartsWith("\\\\") || Same(root, Path.GetPathRoot(root)))
                throw new Failure(21, "UNSAFE_PATH", "Choose a local, writable installation container, not a drive root or network path.");
            NoLinks(root);
            if (o.Qa && !Path.GetFileName(root).StartsWith("syllafolio-qa-", StringComparison.OrdinalIgnoreCase))
                throw new Failure(21, "QA_TARGET_REQUIRED", "The isolated QA container name must start with syllafolio-qa-.");
            if (File.Exists(Long(root)))
                throw new Failure(21, "TARGET_NOT_DIRECTORY", "The selected container is a file.");
            if (Directory.Exists(Long(root)) && Directory.GetFileSystemEntries(Long(root)).Length > 0 && !RootOwned(root, o.Qa))
                throw new Failure(21, "TARGET_NOT_EMPTY", "This folder contains files and is not an authenticated Syllafolio installation container. Choose a new empty folder. Existing files will not be overwritten.");
        }

        static void Shortcut(string file, string target, string args, string working)
        {
            NoLinks(file);
            if (File.Exists(Long(file)))
                throw new Failure(23, "SHORTCUT_EXISTS", "An existing Start Menu shortcut will not be overwritten.");
            Directory.CreateDirectory(Long(Path.GetDirectoryName(file)));
            object shell = null, link = null;
            try
            {
                Type t = Type.GetTypeFromProgID("WScript.Shell", true);
                shell = Activator.CreateInstance(t);
                link = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { file });
                Type lt = link.GetType();
                lt.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { target });
                lt.InvokeMember("Arguments", BindingFlags.SetProperty, null, link, new object[] { args });
                lt.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { working });
                lt.InvokeMember("IconLocation", BindingFlags.SetProperty, null, link, new object[] { target + ",0" });
                lt.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
            }
            finally
            {
                if (link != null)
                    Marshal.FinalReleaseComObject(link);
                if (shell != null)
                    Marshal.FinalReleaseComObject(shell);
            }
        }

        static void Resource(string name, string path, string expected)
        {
            Directory.CreateDirectory(Long(Path.GetDirectoryName(path)));
            string temporary = path + ".part-" + Guid.NewGuid().ToString("N");
            try
            {
                using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
                {
                    if (input == null)
                        throw new Failure(22, "RESOURCE_MISSING", "An installer component is missing.");
                    using (var output = new FileStream(Long(temporary), FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        input.CopyTo(output);
                }

                if (name == "Syllafolio.Uninstall.exe" && FailCacheWrite)
                    throw new Failure(25, "QA_INJECTED_FAILURE", "Explicit QA failure after the private cache temporary copy.");
                if (HashFile(temporary) != expected)
                    throw new Failure(22, "RESOURCE_HASH", "An installer component is corrupt.");
                File.Move(Long(temporary), Long(path));
            }
            finally
            {
                if (File.Exists(Long(temporary)))
                    File.Delete(Long(temporary));
            }
        }

        static void VerifyPayload(FileStream file)
        {
            if (file.Length < Build.ZipLength + 56)
                throw new Failure(22, "PAYLOAD_LENGTH", "The installer is incomplete.");
            file.Position = file.Length - 56;
            byte[] footer = new byte[56];
            if (file.Read(footer, 0, 56) != 56 || Encoding.ASCII.GetString(footer, 0, 16) != Build.FooterMagic || BitConverter.ToInt64(footer, 16) != Build.ZipLength || Hex(footer.Skip(24).ToArray()) != Build.ZipSha)
                throw new Failure(22, "PAYLOAD_FOOTER", "The installer payload footer is corrupt.");
            using (var h = SHA256.Create())
            {
                var s = new SegmentStream(file, file.Length - 56 - Build.ZipLength, Build.ZipLength);
                byte[] b = new byte[1024 * 1024];
                int n;
                long read = 0;
                while ((n = s.Read(b, 0, b.Length)) > 0)
                {
                    CheckCancel();
                    h.TransformBlock(b, 0, n, b, 0);
                    read += n;
                    Progress((int)(read * 10 / Build.ZipLength), "Checking the offline package…");
                }

                h.TransformFinalBlock(new byte[0], 0, 0);
                if (Hex(h.Hash) != Build.ZipSha)
                    throw new Failure(22, "PAYLOAD_HASH", "The offline package SHA-256 does not match this installer.");
            }
        }

        static Dictionary<string, string> Extract(string stage)
        {
            using (var f = new FileStream(Exe, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                VerifyPayload(f);
                using (var z = new ZipArchive(new SegmentStream(f, f.Length - 56 - Build.ZipLength, Build.ZipLength), ZipArchiveMode.Read, true))
                {
                    string prefix = Build.ZipRoot + "/";
                    var info = z.GetEntry(prefix + "INTEGRITY.json");
                    if (info == null)
                        throw new Failure(22, "INTEGRITY_MISSING", "The package integrity manifest is missing.");
                    byte[] raw;
                    using (var s = info.Open())
                    using (var m = new MemoryStream())
                    {
                        s.CopyTo(m);
                        raw = m.ToArray();
                    }

                    if (Hash(raw) != Build.IntegritySha)
                        throw new Failure(22, "INTEGRITY_HASH", "The package integrity manifest differs.");
                    var files = Json.Deserialize<Dictionary<string, string>>(Encoding.UTF8.GetString(raw));
                    if (files.Count != Build.FileCount || z.Entries.Count != files.Count + 1)
                        throw new Failure(22, "PAYLOAD_INVENTORY", "The package inventory is incomplete or has extra members.");
                    int index = 0;
                    long total = 0;
                    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    foreach (var e in z.Entries)
                    {
                        CheckCancel();
                        if (!e.FullName.StartsWith(prefix, StringComparison.Ordinal))
                            throw new Failure(22, "PAYLOAD_PATH", "A package member is outside the expected root.");
                        string name = e.FullName.Substring(prefix.Length);
                        if (!seen.Add(name) || !(files.ContainsKey(name) || name == "INTEGRITY.json"))
                            throw new Failure(22, "PAYLOAD_INVENTORY", "A duplicate or unexpected file was found.");
                        string dest = Safe(stage, name);
                        total += e.Length;
                        if (total > Build.UnpackedBytes)
                            throw new Failure(22, "PAYLOAD_LENGTH", "The extracted package exceeds its declared size.");
                        Directory.CreateDirectory(Long(Path.GetDirectoryName(dest)));
                        using (var input = e.Open())
                        using (var output = new FileStream(Long(dest), FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        using (var h = SHA256.Create())
                        {
                            byte[] b = new byte[1024 * 1024];
                            int n;
                            while ((n = input.Read(b, 0, b.Length)) > 0)
                            {
                                CheckCancel();
                                h.TransformBlock(b, 0, n, b, 0);
                                output.Write(b, 0, n);
                            }

                            h.TransformFinalBlock(new byte[0], 0, 0);
                            string expected = name == "INTEGRITY.json" ? Build.IntegritySha : files[name];
                            if (Hex(h.Hash) != expected)
                                throw new Failure(22, "FILE_HASH", "Extracted file SHA-256 differs: " + name);
                        }

                        index++;
                        if (index % 100 == 0)
                            Progress(10 + (int)(index * 75L / z.Entries.Count), "Installing verified files (" + index + " / " + z.Entries.Count + ")…");
                    }

                    if (total != Build.UnpackedBytes)
                        throw new Failure(22, "PAYLOAD_LENGTH", "The extracted size differs from its declared size.");
                    return files;
                }
            }
        }

        static void DeleteKnown(string root, Dictionary<string, string> files, List<string> preserved, bool skipLock)
        {
            foreach (var p in files.OrderByDescending(x => x.Key.Length))
            {
                if (skipLock && p.Key == ".syllafolio-installation.lock")
                    continue;
                string path = Safe(root, p.Key);
                if (File.Exists(Long(path)))
                {
                    if (HashFile(path) == p.Value)
                        File.Delete(Long(path));
                    else
                        preserved.Add(path);
                }
            }
        }

        static void EmptyKnownDirectories(string root, IEnumerable<string> names)
        {
            var dirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string n in names)
            {
                string p = Path.GetDirectoryName(Safe(root, n));
                while (p != null && p.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                {
                    dirs.Add(p);
                    p = Path.GetDirectoryName(p);
                }
            }

            foreach (string p in dirs.OrderByDescending(x => x.Length))
                if (Directory.Exists(Long(p)) && Directory.GetFileSystemEntries(Long(p)).Length == 0)
                    Directory.Delete(Long(p));
            if (Directory.Exists(Long(root)) && Directory.GetFileSystemEntries(Long(root)).Length == 0)
                Directory.Delete(Long(root));
        }

        static void RemoveStage(string stage)
        {
            if (stage == null || !Directory.Exists(Long(stage)))
                return;
            NoLinks(stage);
            foreach (string file in Directory.GetFiles(Long(stage), "*", SearchOption.AllDirectories))
            {
                NoLinks(file);
                File.Delete(file);
            }

            foreach (string d in Directory.GetDirectories(Long(stage), "*", SearchOption.AllDirectories).OrderByDescending(x => x.Length))
            {
                NoLinks(d);
                Directory.Delete(d);
            }

            Directory.Delete(Long(stage));
        }

        static void ClearEmptyContainer(string root, bool qa)
        {
            if (!Directory.Exists(Long(root)))
                return;
            if (RootOwned(root, qa) && Directory.GetFileSystemEntries(Long(root)).All(p => Same(p, Path.Combine(root, Marker))))
            {
                File.Delete(Long(Path.Combine(root, Marker)));
                Directory.Delete(Long(root));
                Registry.CurrentUser.DeleteSubKeyTree(RootKey(root), false);
            }
        }

        internal static Dictionary<string, object> Install(Options o)
        {
            if (!Build.CanInstall)
                throw new Failure(2, "UNINSTALLER_ONLY", "This component only uninstalls Syllafolio.");
            string root = Full(o.Target ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Syllafolio"));
            using (new ProductMutex(o.Qa, false))
            {
                using (var k = Registry.CurrentUser.OpenSubKey(Key(o.Qa)))
                    if (k != null)
                        throw new Failure(23, "ALREADY_INSTALLED", "This product is already installed. Uninstall it normally from Windows Settings or the Start Menu before installing again.");
                RequireContainer(root, o);
                var drive = new DriveInfo(Path.GetPathRoot(root));
                if (drive.AvailableFreeSpace < Build.UnpackedBytes + 100L * 1024 * 1024)
                    throw new Failure(25, "DISK_SPACE", "Not enough free space is available. Free some disk space or choose another local location. Installation has not been registered.");
                string group = Group(o.Qa);
                NoLinks(group);
                if (Directory.Exists(Long(group)) && Directory.GetFileSystemEntries(Long(group)).Length > 0)
                    throw new Failure(23, "START_MENU_EXISTS", "The product Start Menu group already contains entries; it will not be replaced.");
                string instance = Guid.NewGuid().ToString("N").Substring(0, 8), app = Path.Combine(root, "app-" + instance), stage = null, cache = Cache(o.Qa);
                bool ownerCreated = false, published = false, registered = false, cacheCreated = false;
                var links = new Dictionary<string, string>();
                Dictionary<string, string> files = null;
                try
                {
                    Directory.CreateDirectory(Long(root));
                    if (!RootOwned(root, o.Qa))
                    {
                        RegisterRoot(root, o.Qa);
                        ownerCreated = true;
                    }

                    string stagingCandidate = Path.Combine(root, ".stage-" + instance);
                    CreateStageDirectory(stagingCandidate);
                    stage = stagingCandidate;
                    files = Extract(stage);
                    Resource("Syllafolio.Launcher.exe", Path.Combine(stage, "Syllafolio.exe"), Build.LauncherSha);
                    files["Syllafolio.exe"] = Build.LauncherSha;
                    Save(Path.Combine(stage, "INTEGRITY.json"), files);
                    files = new Dictionary<string, string>(files);
                    files["INTEGRITY.json"] = HashFile(Path.Combine(stage, "INTEGRITY.json"));
                    Save(Path.Combine(stage, Pending), new { schema = "syllafolio-install-pending-v1", phase = "registration" });
                    Directory.CreateDirectory(Long(Path.GetDirectoryName(cache)));
                    NoLinks(cache);
                    if (File.Exists(Long(cache)))
                    {
                        if (HashFile(cache) != Build.UninstallSha)
                            throw new Failure(23, "UNINSTALL_CACHE_CONFLICT", "The private uninstaller cache has unexpected bytes; it will not be overwritten.");
                    }
                    else
                    {
                        Resource("Syllafolio.Uninstall.exe", cache, Build.UninstallSha);
                        cacheCreated = true;
                    }

                    CheckCancel();
                    FailAt(o, "before_publish");
                    Progress(88, "Completing installation registration…");
                    Directory.Move(Long(stage), Long(app));
                    stage = null;
                    published = true;
                    FailAt(o, "after_publish");
                    string command = Q(cache) + " --uninstall --target " + Q(app) + (o.Qa ? " --qa" : "");
                    Shortcut(Path.Combine(group, "Syllafolio.lnk"), Path.Combine(app, "Syllafolio.exe"), "", app);
                    links[Path.Combine(group, "Syllafolio.lnk")] = HashFile(Path.Combine(group, "Syllafolio.lnk"));
                    Shortcut(Path.Combine(group, "Uninstall Syllafolio.lnk"), cache, "--uninstall --target " + Q(app) + (o.Qa ? " --qa" : ""), app);
                    links[Path.Combine(group, "Uninstall Syllafolio.lnk")] = HashFile(Path.Combine(group, "Uninstall Syllafolio.lnk"));
                    var manifest = new InstallManifest
                    {
                        schema = "syllafolio-current-user-install-v1",
                        product = Product,
                        version = Build.Version,
                        qa = o.Qa,
                        container = root,
                        app_root = app,
                        instance = instance,
                        cache_path = cache,
                        cache_sha256 = Build.UninstallSha,
                        files = files,
                        shortcuts = links
                    };
                    Save(Path.Combine(app, Manifest), manifest);
                    using (var k = Registry.CurrentUser.CreateSubKey(Key(o.Qa)))
                    {
                        registered = true;
                        k.SetValue("DisplayName", o.Qa ? "Syllafolio Lite QA" : "Syllafolio");
                        k.SetValue("DisplayVersion", Build.Version);
                        k.SetValue("Publisher", "Syllafolio");
                        k.SetValue("InstallLocation", app);
                        k.SetValue("Container", root);
                        k.SetValue("DisplayIcon", Path.Combine(app, "Syllafolio.exe") + ",0");
                        k.SetValue("UninstallString", command);
                        k.SetValue("QuietUninstallString", command + " --quiet");
                        k.SetValue("ManifestSha256", HashFile(Path.Combine(app, Manifest)));
                        k.SetValue("Product", Product);
                        k.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture));
                        k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                        k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                        k.SetValue("EstimatedSize", (int)(Build.UnpackedBytes / 1024), RegistryValueKind.DWord);
                    }

                    FailAt(o, "after_registry");
                    File.Delete(Long(Path.Combine(app, Pending)));
                    Progress(100, "Installed. Open Syllafolio from the Start Menu.");
                    return new Dictionary<string, object>
                    {
                        {
                            "status",
                            "passed"
                        },
                        {
                            "operation",
                            "install"
                        },
                        {
                            "target",
                            root
                        },
                        {
                            "container",
                            root
                        },
                        {
                            "app_root",
                            app
                        },
                        {
                            "version",
                            Build.Version
                        },
                        {
                            "qa",
                            o.Qa
                        },
                        {
                            "registry_key",
                            "HKCU\\" + Key(o.Qa)
                        },
                        {
                            "start_menu_group",
                            group
                        },
                        {
                            "uninstall_string",
                            command
                        },
                        {
                            "manifest_sha256",
                            HashFile(Path.Combine(app, Manifest))
                        },
                        {
                            "payload_sha256",
                            Build.ZipSha
                        },
                        {
                            "launcher_sha256",
                            Build.LauncherSha
                        },
                        {
                            "uninstaller_sha256",
                            Build.UninstallSha
                        },
                        {
                            "application_files",
                            files.Count
                        },
                        {
                            "rollback",
                            false
                        }
                    };
                }
                catch
                {
                    if (registered)
                        Registry.CurrentUser.DeleteSubKeyTree(Key(o.Qa), false);
                    foreach (var p in links)
                        if (File.Exists(Long(p.Key)) && HashFile(p.Key) == p.Value)
                            File.Delete(Long(p.Key));
                    if (Directory.Exists(Long(group)) && Directory.GetFileSystemEntries(Long(group)).Length == 0)
                        Directory.Delete(Long(group));
                    if (published && files != null)
                    {
                        var preserve = new List<string>();
                        DeleteKnown(app, files, preserve, false);
                        foreach (string name in new[]
                        {
                            Manifest,
                            Pending
                        }

                        )
                        {
                            string p = Path.Combine(app, name);
                            if (File.Exists(Long(p)))
                                File.Delete(Long(p));
                        }

                        EmptyKnownDirectories(app, files.Keys);
                    }

                    if (stage != null)
                        RemoveStage(stage);
                    if (cacheCreated && File.Exists(Long(cache)) && HashFile(cache) == Build.UninstallSha)
                        File.Delete(Long(cache));
                    if (ownerCreated)
                        ClearEmptyContainer(root, o.Qa);
                    throw;
                }
            }
        }

        internal static Dictionary<string, object> Uninstall(Options o)
        {
            if (o.Target == null)
                throw new Failure(2, "UNINSTALL_TARGET_REQUIRED", "Uninstall requires the registered application path.");
            string app = Full(o.Target);
            NoLinks(app);
            string path = Path.Combine(app, Manifest);
            InstallManifest m;
            using (new ProductMutex(o.Qa, false))
            {
                using (var key = Registry.CurrentUser.OpenSubKey(Key(o.Qa)))
                {
                    if (key == null || Value(key, "Product") != Product || !Same(Value(key, "InstallLocation"), app) || !File.Exists(Long(path)) || HashFile(path) != Value(key, "ManifestSha256"))
                        throw new Failure(21, "UNTRUSTED_INSTALLATION", "The selected installation does not match its current-user uninstall registration. No files were removed.");
                    m = Json.Deserialize<InstallManifest>(File.ReadAllText(Long(path), Encoding.UTF8));
                }

                if (m.schema != "syllafolio-current-user-install-v1" || m.product != Product || m.qa != o.Qa || !Same(m.app_root, app) || !RootOwned(m.container, o.Qa) || !Same(Path.GetDirectoryName(app), m.container) || !Path.GetFileName(app).StartsWith("app-", StringComparison.Ordinal))
                    throw new Failure(21, "UNTRUSTED_INSTALLATION", "The installation metadata does not match its authenticated container.");
                var preserved = new List<string>();
                Progress(5, "Checking that this installation is closed…");
                using (new InstallationLock(app))
                {
                    foreach (var pair in m.files)
                        Safe(app, pair.Key);
                    foreach (var shortcut in m.shortcuts)
                    {
                        if (!Same(Path.GetDirectoryName(shortcut.Key), Group(o.Qa)))
                            throw new Failure(21, "UNTRUSTED_SHORTCUT", "The registered shortcut is outside this product's group.");
                        NoLinks(shortcut.Key);
                    }

                    Save(Path.Combine(app, Pending), new { schema = "syllafolio-uninstall-pending-v1", phase = "uninstall" });
                    DeleteKnown(app, m.files, preserved, true);
                    FailAt(o, "uninstall_files");
                }

                string lockPath = Path.Combine(app, ".syllafolio-installation.lock");
                if (File.Exists(Long(lockPath)) && HashFile(lockPath) == m.files[".syllafolio-installation.lock"])
                    File.Delete(Long(lockPath));
                foreach (var p in m.shortcuts)
                {
                    if (!Same(Path.GetDirectoryName(p.Key), Group(o.Qa)))
                        throw new Failure(21, "UNTRUSTED_SHORTCUT", "The registered shortcut is outside this product's group.");
                    NoLinks(p.Key);
                    if (File.Exists(Long(p.Key)))
                    {
                        if (HashFile(p.Key) == p.Value)
                            File.Delete(Long(p.Key));
                        else
                            preserved.Add(p.Key);
                    }
                }

                string group = Group(o.Qa);
                if (Directory.Exists(Long(group)) && Directory.GetFileSystemEntries(Long(group)).Length == 0)
                    Directory.Delete(Long(group));
                File.Delete(Long(path));
                Registry.CurrentUser.DeleteSubKeyTree(Key(o.Qa), false);
                File.Delete(Long(Path.Combine(app, Pending)));
                EmptyKnownDirectories(app, m.files.Keys);
                ClearEmptyContainer(m.container, o.Qa);
                Progress(100, "Uninstalled. User files and changed files were preserved.");
                return new Dictionary<string, object>
                {
                    {
                        "status",
                        "passed"
                    },
                    {
                        "operation",
                        "uninstall"
                    },
                    {
                        "target",
                        app
                    },
                    {
                        "app_root",
                        app
                    },
                    {
                        "container",
                        m.container
                    },
                    {
                        "qa",
                        o.Qa
                    },
                    {
                        "preserved_modified_files",
                        preserved
                    },
                    {
                        "residual_directory_exists",
                        Directory.Exists(Long(app))
                    },
                    {
                        "cache_path",
                        m.cache_path
                    },
                    {
                        "cache_sha256",
                        m.cache_sha256
                    },
                    {
                        "registry_removed",
                        true
                    },
                    {
                        "start_menu_removed",
                        !Directory.Exists(Long(group))
                    }
                };
            }
        }

        internal static void ScheduleCleanup(Options o, Dictionary<string, object> receipt)
        {
            receipt["cache_cleanup_scheduled"] = false;
            try
            {
                FailAt(o, "cleanup_cache");
                string cache = Convert.ToString(receipt["cache_path"]);
                if (!File.Exists(Long(cache)) || HashFile(cache) != Convert.ToString(receipt["cache_sha256"]))
                {
                    receipt["cache_cleanup_note"] = "The cache is absent or has changed; no cache file was removed.";
                    return;
                }

                string folder = Path.Combine(Path.GetTempPath(), "SyllafolioInstallerCleanup", Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(Long(folder));
                string helper = Path.Combine(folder, "cleanup.exe");
                File.Copy(Long(cache), Long(helper), false);
                var start = new ProcessStartInfo(helper, "--cleanup-cache --target " + Q(cache) + " --expected-sha " + Q(HashFile(cache)) + " --wait-pid " + Process.GetCurrentProcess().Id + (o.Qa ? " --qa" : ""))
                {
                    UseShellExecute = false,
                    CreateNoWindow = true
                };
                Process.Start(start);
                receipt["cache_cleanup_scheduled"] = true;
                receipt["temporary_cleanup_helper"] = helper;
            }
            catch (Exception error)
            {
                // Application removal already committed. Cache housekeeping must
                // not turn that successful uninstall into an unrecorded failure.
                receipt["cache_cleanup_error"] = UserMessage(error);
            }
        }

        internal static int Cleanup(Options o)
        {
            try
            {
                if (o.WaitPid > 0)
                {
                    try
                    {
                        Process.GetProcessById(o.WaitPid).WaitForExit(60000);
                    }
                    catch (ArgumentException)
                    {
                    }
                }

                using (new ProductMutex(o.Qa, true))
                {
                    if (!Same(o.Target, Cache(o.Qa)) || o.ExpectedSha != HashFile(Exe))
                        return 21;
                    using (var k = Registry.CurrentUser.OpenSubKey(Key(o.Qa)))
                        if (k != null)
                            return 0;
                    NoLinks(o.Target);
                    if (File.Exists(Long(o.Target)) && HashFile(o.Target) == o.ExpectedSha)
                        File.Delete(Long(o.Target));
                }

                return 0;
            }
            catch
            {
                return 25;
            }
        }

        internal static Dictionary<string, object> Run(Options o)
        {
            FailCacheWrite = o.Qa && o.FailStage == "cache_write";
            return o.Uninstall ? Uninstall(o) : Install(o);
        }

        internal static void Receipt(Options o, Dictionary<string, object> result, int exit)
        {
            result["schema"] = "syllafolio-installer-receipt-v1";
            result["exit_code"] = exit;
            result["utc"] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
            string p = o.Receipt ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Syllafolio", "Installer", "logs", DateTime.UtcNow.ToString("yyyyMMddTHHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".json");
            if (File.Exists(Long(p)))
                throw new IOException("Refusing to overwrite an existing receipt: " + p);
            Save(p, result);
        }
    }

    static class EnglishDialogs
    {
        internal static void Error(string message)
        {
            using (var dialog = new Form())
            {
                dialog.Text = "Syllafolio Setup";
                dialog.Font = new Font("Segoe UI", 10);
                dialog.AutoScaleMode = AutoScaleMode.Dpi;
                dialog.StartPosition = FormStartPosition.CenterScreen;
                dialog.ClientSize = new Size(560, 240);
                dialog.MinimumSize = new Size(480, 220);
                var details = new TextBox
                {
                    Text = message,
                    ReadOnly = true,
                    Multiline = true,
                    ScrollBars = ScrollBars.Vertical,
                    Location = new Point(20, 20),
                    Size = new Size(520, 155),
                    Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right
                };
                var ok = new Button
                {
                    Text = "OK",
                    DialogResult = DialogResult.OK,
                    Location = new Point(445, 190),
                    Size = new Size(95, 32),
                    Anchor = AnchorStyles.Bottom | AnchorStyles.Right
                };
                dialog.Controls.Add(details);
                dialog.Controls.Add(ok);
                dialog.AcceptButton = ok;
                dialog.CancelButton = ok;
                dialog.ShowDialog();
            }
        }

        internal static string ChooseLocation(IWin32Window owner, string current)
        {
            using (var dialog = new Form())
            {
                dialog.Text = "Choose installation location";
                dialog.Font = new Font("Segoe UI", 10);
                dialog.AutoScaleMode = AutoScaleMode.Dpi;
                dialog.StartPosition = FormStartPosition.CenterParent;
                dialog.ClientSize = new Size(620, 210);
                dialog.MinimumSize = new Size(520, 240);
                var help = new Label
                {
                    Text = "Enter the full path of an empty local folder or an authenticated Syllafolio container. A new folder will be created if needed.",
                    Location = new Point(20, 20),
                    Size = new Size(580, 55),
                    Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
                };
                var path = new TextBox
                {
                    Text = current,
                    Location = new Point(20, 90),
                    Size = new Size(580, 28),
                    Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
                };
                var choose = new Button
                {
                    Text = "Use this folder",
                    Location = new Point(360, 155),
                    Size = new Size(130, 32),
                    Anchor = AnchorStyles.Bottom | AnchorStyles.Right
                };
                var cancel = new Button
                {
                    Text = "Cancel",
                    DialogResult = DialogResult.Cancel,
                    Location = new Point(505, 155),
                    Size = new Size(95, 32),
                    Anchor = AnchorStyles.Bottom | AnchorStyles.Right
                };
                choose.Click += delegate
                {
                    if (string.IsNullOrWhiteSpace(path.Text) || !Path.IsPathRooted(path.Text))
                    {
                        Error("Enter a full local folder path, for example a folder under your user account.");
                        return;
                    }

                    dialog.DialogResult = DialogResult.OK;
                    dialog.Close();
                };
                dialog.Controls.Add(help);
                dialog.Controls.Add(path);
                dialog.Controls.Add(choose);
                dialog.Controls.Add(cancel);
                dialog.AcceptButton = choose;
                dialog.CancelButton = cancel;
                return dialog.ShowDialog(owner) == DialogResult.OK ? path.Text : null;
            }
        }
    }

    sealed class Wizard : Form
    {
        Options options;
        TextBox target;
        Button action, cancel, browse;
        Label status;
        ProgressBar progress;
        CheckBox launch;
        bool running;
        internal int ExitCode = 24;
        internal Wizard(Options o)
        {
            options = o;
            Text = o.Uninstall ? "Uninstall Syllafolio" : "Syllafolio Setup";
            Font = new Font("Segoe UI", 10);
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(640, 390);
            MinimumSize = new Size(590, 420);
            StartPosition = FormStartPosition.CenterScreen;
            var title = new Label
            {
                Text = o.Uninstall ? "Uninstall Syllafolio" : "Install Syllafolio " + Build.Version,
                AutoSize = true,
                Font = new Font("Segoe UI", 15, FontStyle.Bold),
                Location = new Point(25, 22)
            };
            Controls.Add(title);
            var info = new Label
            {
                Text = o.Uninstall ? "Close this installation first. Uninstall preserves projects, logs and changed files in their original locations." : "Offline installation for your Windows account. No administrator access is required. Starts with an empty workspace; no samples are included. Existing projects and other installations are preserved.",
                AutoSize = false,
                Location = new Point(25, 67),
                Size = new Size(585, 58),
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
            };
            Controls.Add(info);
            Controls.Add(new Label { Text = o.Uninstall ? "Registered application folder" : "Installation location", AutoSize = true, Location = new Point(25, 137) });
            target = new TextBox
            {
                Text = o.Target ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Syllafolio"),
                Location = new Point(25, 164),
                Size = new Size(490, 27),
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
                ReadOnly = o.Uninstall
            };
            Controls.Add(target);
            browse = new Button
            {
                Text = "Change…",
                Location = new Point(525, 162),
                Size = new Size(87, 31),
                Anchor = AnchorStyles.Top | AnchorStyles.Right,
                Enabled = !o.Uninstall
            };
            browse.Click += delegate
            {
                string chosen = EnglishDialogs.ChooseLocation(this, target.Text);
                if (chosen != null)
                    target.Text = chosen;
            };
            Controls.Add(browse);
            status = new Label
            {
                Text = "Program files use a separate app subfolder. Retained data is never overwritten.",
                Location = new Point(25, 210),
                Size = new Size(585, 57),
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
            };
            Controls.Add(status);
            progress = new ProgressBar
            {
                Location = new Point(25, 280),
                Size = new Size(585, 18),
                Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right
            };
            Controls.Add(progress);
            launch = new CheckBox
            {
                Text = "Open Syllafolio after installation",
                Checked = false,
                Visible = !o.Uninstall,
                AutoSize = true,
                Location = new Point(25, 317),
                Anchor = AnchorStyles.Bottom | AnchorStyles.Left
            };
            Controls.Add(launch);
            action = new Button
            {
                Text = o.Uninstall ? "Uninstall" : "Install",
                Location = new Point(410, 339),
                Size = new Size(95, 32),
                Anchor = AnchorStyles.Bottom | AnchorStyles.Right
            };
            cancel = new Button
            {
                Text = "Cancel",
                Location = new Point(515, 339),
                Size = new Size(95, 32),
                Anchor = AnchorStyles.Bottom | AnchorStyles.Right
            };
            Controls.Add(action);
            Controls.Add(cancel);
            AcceptButton = action;
            CancelButton = cancel;
            action.Click += delegate
            {
                Start();
            };
            cancel.Click += delegate
            {
                if (running)
                {
                    Engine.Cancelled = true;
                    status.Text = "Cancelling safely…";
                }
                else
                    Close();
            };
            FormClosing += delegate (object s, FormClosingEventArgs e)
            {
                if (running)
                {
                    Engine.Cancelled = true;
                    e.Cancel = true;
                    status.Text = "Waiting for safe cancellation…";
                }
            };
            try
            {
                Icon = Icon.ExtractAssociatedIcon(Engine.Exe);
            }
            catch
            {
            }
        }

        void Start()
        {
            options.Target = target.Text;
            running = true;
            action.Enabled = false;
            target.Enabled = false;
            browse.Enabled = false;
            launch.Enabled = false;
            Engine.Cancelled = false;
            var worker = new BackgroundWorker
            {
                WorkerReportsProgress = true
            };
            Engine.Progress = delegate (int p, string text)
            {
                worker.ReportProgress(p, text);
            };
            worker.ProgressChanged += delegate (object s, ProgressChangedEventArgs e)
            {
                progress.Value = Math.Max(0, Math.Min(100, e.ProgressPercentage));
                status.Text = Convert.ToString(e.UserState);
                if (e.ProgressPercentage >= 88)
                    cancel.Enabled = false;
            };
            worker.DoWork += delegate (object s, DoWorkEventArgs e)
            {
                e.Result = Engine.Run(options);
            };
            worker.RunWorkerCompleted += delegate (object s, RunWorkerCompletedEventArgs e)
            {
                running = false;
                cancel.Enabled = true;
                Dictionary<string, object> r;
                int code;
                if (e.Error != null)
                {
                    var failure = e.Error as Failure;
                    code = failure == null ? 25 : failure.Exit;
                    r = new Dictionary<string, object>
                    {
                        {
                            "status",
                            "failed"
                        },
                        {
                            "code",
                            failure == null ? "IO_FAILURE" : failure.Code
                        },
                        {
                            "message",
                            Engine.UserMessage(e.Error)
                        },
                        {
                            "target",
                            options.Target
                        },
                        {
                            "qa",
                            options.Qa
                        }
                    };
                    status.Text = Engine.UserMessage(e.Error);
                }
                else
                {
                    code = 0;
                    r = (Dictionary<string, object>)e.Result;
                    status.Text = options.Uninstall ? "Uninstalled. Retained data remains in its original location." : "Installed. Open Syllafolio from the Start Menu.";
                    if (options.Uninstall)
                        Engine.ScheduleCleanup(options, r);
                }

                try
                {
                    Engine.Receipt(options, r, code);
                }
                catch (Exception error)
                {
                    code = 25;
                    status.Text += " Receipt error: " + Engine.UserMessage(error);
                }

                ExitCode = code;
                action.Visible = false;
                cancel.Text = "Close";
                if (code == 0 && !options.Uninstall && launch.Checked)
                {
                    try
                    {
                        Process.Start(new ProcessStartInfo(Path.Combine(Convert.ToString(r["app_root"]), "Syllafolio.exe")) { UseShellExecute = true, WorkingDirectory = Convert.ToString(r["app_root"]) });
                    }
                    catch (Exception error)
                    {
                        status.Text += " Launch: " + Engine.UserMessage(error);
                    }
                }
            };
            worker.RunWorkerAsync();
        }
    }

    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Thread.CurrentThread.CurrentUICulture = CultureInfo.GetCultureInfo("en-US");
            CultureInfo.DefaultThreadCurrentUICulture = CultureInfo.GetCultureInfo("en-US");
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
            Options o = null;
            try
            {
                o = Options.Parse(args);
                if (o.Cleanup)
                    return Engine.Cleanup(o);
                if (!o.Quiet)
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    using (var window = new Wizard(o))
                    {
                        Application.Run(window);
                        return window.ExitCode;
                    }
                }

                var r = Engine.Run(o);
                if (o.Uninstall)
                    Engine.ScheduleCleanup(o, r);
                Engine.Receipt(o, r, 0);
                return 0;
            }
            catch (Exception error)
            {
                var f = error as Failure;
                int exit = f == null ? 25 : f.Exit;
                if (o != null)
                {
                    try
                    {
                        Engine.Receipt(o, new Dictionary<string, object> { { "status", "failed" }, { "code", f == null ? "IO_FAILURE" : f.Code }, { "message", Engine.UserMessage(error) }, { "target", o.Target }, { "qa", o.Qa } }, exit);
                    }
                    catch
                    {
                    }

                    if (!o.Quiet)
                        EnglishDialogs.Error(Engine.UserMessage(error));
                }
                else if (!args.Contains("--quiet"))
                    EnglishDialogs.Error(Engine.UserMessage(error));
                return exit;
            }
        }
    }
}
