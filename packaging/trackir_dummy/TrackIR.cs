// OpenITrack's stand-in "TrackIR.exe".
//
// A few games only turn on TrackIR support while a process named TrackIR.exe is
// running. OpenITrack starts this program next to its client DLLs while the
// Direct game output runs. It has no window and touches no files or network:
// it only waits for the process whose ID it was given (OpenITrack) to exit, so
// it also goes away if OpenITrack crashes or is killed.
//
// Build (packaging/build.py does this, with the csc.exe that ships with Windows):
//   csc /target:winexe /out:TrackIR.exe TrackIR.cs

using System;
using System.Diagnostics;
using System.Reflection;

[assembly: AssemblyTitle("OpenITrack TrackIR stand-in")]
[assembly: AssemblyDescription("Keeps a TrackIR.exe process running while OpenITrack's Direct game output is on")]
[assembly: AssemblyProduct("OpenITrack")]

static class TrackIRDummy
{
    static int Main(string[] args)
    {
        int parentId;
        if (args.Length != 1 || !int.TryParse(args[0], out parentId))
            return 2;  // usage: TrackIR.exe <OpenITrack process ID>
        try
        {
            Process.GetProcessById(parentId).WaitForExit();
        }
        catch (Exception)
        {
            return 1;  // already gone, or not a process we may wait for
        }
        return 0;
    }
}
