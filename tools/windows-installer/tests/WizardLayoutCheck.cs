// Compile this probe with the same x64 target and DPI manifest as Setup.
// It opens its own copies of the release forms and never invokes the installer engine.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal static class WizardLayoutCheck
{
    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr window);

    private static IEnumerable<Control> Children(Control parent)
    {
        foreach (Control child in parent.Controls)
        {
            yield return child;
            foreach (Control descendant in Children(child)) yield return descendant;
        }
    }

    private static void Check(Form form, bool small)
    {
        Application.DoEvents();
        var client = form.RectangleToScreen(form.ClientRectangle);
        if (!Screen.FromControl(form).WorkingArea.Contains(form.Bounds))
            throw new InvalidOperationException("The dialog exceeds the monitor work area.");
        var scroll = Children(form).OfType<Panel>().FirstOrDefault(p => p.AutoScroll);
        foreach (var c in Children(form).Where(c => c.Visible && (c is Label || c is Button || c is CheckBox)))
        {
            Size needed = c.GetPreferredSize(new Size(c.Width, 0));
            if (c.Height < needed.Height || (!(c is Label) && c.Width < needed.Width))
                throw new InvalidOperationException("Clipped text: " + c.Text);
            var box = c.RectangleToScreen(c.ClientRectangle);
            if (scroll != null && scroll.Contains(c))
            {
                var visible = scroll.RectangleToScreen(scroll.ClientRectangle);
                if (small && !visible.Contains(box))
                {
                    scroll.ScrollControlIntoView(c);
                    Application.DoEvents();
                    box = c.RectangleToScreen(c.ClientRectangle);
                    bool reached = box.Height <= visible.Height ? visible.Contains(box) : visible.IntersectsWith(box);
                    if (!reached) throw new InvalidOperationException("Unreachable: " + c.Text + " " + box + " in " + visible
                        + "; extent " + scroll.AutoScrollMinSize + ", max=" + scroll.VerticalScroll.Maximum
                        + ", page=" + scroll.VerticalScroll.LargeChange + ", pos=" + scroll.VerticalScroll.Value);
                }
                else if (!visible.Contains(box))
                    throw new InvalidOperationException("Clipped by viewport: " + c.Text);
            }
            else if (!client.Contains(box))
                throw new InvalidOperationException("Action outside dialog: " + c.Text);
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception e) { Console.Error.WriteLine(e.GetType().Name + ": " + e.Message); return 1; }
    }

    private static int Run(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var assembly = Assembly.LoadFile(Path.GetFullPath(args[0]));
        var flags = BindingFlags.Public | BindingFlags.NonPublic;
        var parse = assembly.GetType("SyllafolioInstaller.Options", true).GetMethod("Parse", flags | BindingFlags.Static);
        foreach (bool uninstall in new[] { false, true })
        {
            string[] argv = uninstall ? new[] { "--uninstall", "--target", Path.GetTempPath() } : new string[0];
            var options = parse.Invoke(null, new object[] { argv });
            using (var form = (Form)Activator.CreateInstance(assembly.GetType("SyllafolioInstaller.Wizard", true),
                    flags | BindingFlags.Instance, null, new object[] { options }, CultureInfo.InvariantCulture))
            {
                form.ShowInTaskbar = false;
                form.Opacity = 0;
                form.Show();
                uint dpi = GetDpiForWindow(form.Handle);
                if (args.Length > 1 && dpi != uint.Parse(args[1], CultureInfo.InvariantCulture))
                    throw new InvalidOperationException("Unexpected test-window DPI: " + dpi);
                Console.WriteLine("{0}: {1} DPI, {2} client", form.Text, dpi, form.ClientSize);
                var desktop = Children(form).OfType<CheckBox>().Single(c => c.Text == "Create a desktop shortcut");
                if (desktop.Visible != !uninstall || (desktop.Visible && !desktop.Checked))
                    throw new InvalidOperationException("Unexpected desktop shortcut option.");
                var scroll = Children(form).OfType<Panel>().First(p => p.AutoScroll);
                Check(form, form.Height >= Screen.FromControl(form).WorkingArea.Height && scroll.VerticalScroll.Visible);
                form.Width = form.MinimumSize.Width;
                Console.WriteLine("Checking narrow width: " + form.ClientSize);
                Check(form, true);
                form.Size = form.MinimumSize;
                Console.WriteLine("Checking minimum size: " + form.ClientSize);
                Check(form, true);
                form.Close();
            }
        }
        return 0;
    }
}
