;;; Local runtime configuration, inspection, and locked launch decisions.

(in-package #:lisp-mcp)

#+linux (eval-when (:compile-toplevel :load-toplevel :execute) (require :sb-posix))

(defstruct (genera-runtime (:constructor %make-genera-runtime))
  (mode :attach) autostart root executable world partition debugger user
  network tap guest gateway state-directory log)

(defun genera-runtime-path-option (name &optional default absolute)
  (let ((text (configuration-text name default)))
    (when text
      (unless (and (not (find #\\ text))
                   (not (and (>= (length text) 2) (alpha-char-p (char text 0))
                             (char= (char text 1) #\:)))
                   (or (not absolute) (char= (char text 0) #\/))
                   (handler-case (not (wild-pathname-p (pathname text)))
                     (error () nil)))
        (configuration-error name "must be a literal Unix pathname; use WSL paths")))
    text))

(defun genera-ipv4-p (text)
  (let ((parts (uiop:split-string text :separator ".")))
    (and (= (length parts) 4)
         (every (lambda (part)
                  (and (<= 1 (length part) 3)
                       (every (lambda (ch) (char<= #\0 ch #\9)) part)
                       (<= (parse-integer part) 255)
                       (string= part (format nil "~D" (parse-integer part))))) parts))))

(defun genera-parse-runtime-network (text)
  ;; Preserve the validated argument verbatim, including its guest netmask.
  (let* ((parts (and text (uiop:split-string text :separator ";")))
         (first (first parts)) (colon (and first (position #\: first)))
         (tap (and colon (subseq first 0 colon)))
         (guest (and colon (subseq first (1+ colon))))
         (mask (and (= (length parts) 4) (uiop:string-prefix-p "mask=" (second parts))
                    (subseq (second parts) 5)))
         (gateway (and (= (length parts) 4) (uiop:string-prefix-p "gateway=" (third parts))
                       (subseq (third parts) 8))))
    (unless (and tap (<= 1 (length tap) 15) (not (member tap '("." "..") :test #'equal))
                 (every (lambda (ch) (or (find ch "-_.")
                                         (char<= #\a ch #\z) (char<= #\A ch #\Z)
                                         (char<= #\0 ch #\9))) tap)
                 guest (genera-ipv4-p guest) mask (genera-ipv4-p mask)
                 gateway (genera-ipv4-p gateway)
                 (equal (fourth parts) "preconfiguredTAP=yes"))
      (configuration-error "GENERA_VLM_NETWORK"
                           "requires TAP:IPv4;mask=IPv4;gateway=IPv4;preconfiguredTAP=yes"))
    (values tap guest gateway)))

(defun genera-runtime-configuration (profile protocol host port)
  "Read syntax and policy only. Do not inspect files, connect, or launch."
  (let* ((mode (configuration-choice "GENERA_RUNTIME_MODE" "attach" '("attach" "local")))
         (autostart (equal (configuration-choice "GENERA_AUTOSTART" "false" '("false" "true")) "true"))
         (root (genera-runtime-path-option "GENERA_VLM_ROOT" nil t))
         (executable (genera-runtime-path-option "GENERA_VLM_EXECUTABLE" "genera"))
         (world (genera-runtime-path-option "GENERA_VLM_WORLD"))
         (partition (genera-runtime-path-option "GENERA_VLM_PARTITION"))
         (debugger (genera-runtime-path-option "GENERA_VLM_DEBUGGER" "VLM_debugger"))
         (user (configuration-text "GENERA_VLM_USER"))
         (network (configuration-text "GENERA_VLM_NETWORK"))
         (state (genera-runtime-path-option
                 "GENERA_STATE_DIR"
                 #+linux (namestring (merge-pathnames "lisp-mcp/"
                   (uiop:ensure-directory-pathname
                    (or (genera-runtime-path-option "XDG_STATE_HOME" nil t)
                        (merge-pathnames ".local/state/" (user-homedir-pathname))))))
                 #-linux nil t))
         (log (genera-runtime-path-option "GENERA_VLM_LOG"
                  (when state (namestring (merge-pathnames "vlm.log" (uiop:ensure-directory-pathname state)))) t)))
    (when (and autostart (equal mode "attach"))
      (configuration-error "GENERA_AUTOSTART" "requires GENERA_RUNTIME_MODE=local; remote targets are attach-only"))
    (when (equal mode "local")
      (unless (and (equal profile "genera") (equal protocol "v1"))
        (configuration-error "GENERA_RUNTIME_MODE" "local requires the genera profile and protocol v1"))
      (loop for (name value) on (list "GENERA_VLM_ROOT" root "GENERA_VLM_WORLD" world
                                      "GENERA_VLM_PARTITION" partition "GENERA_VLM_USER" user
                                      "GENERA_VLM_NETWORK" network) by #'cddr
            unless value do (configuration-error name "is required for the local runtime"))
      (unless (= port 8888)
        (configuration-error "GENERA_PORT" "the local boot listener requires port 8888")))
    (multiple-value-bind (tap guest gateway)
        (when network (genera-parse-runtime-network network))
      (when (and (equal mode "local") (not (equal host guest)))
        (configuration-error "GENERA_HOST" "must equal the guest IPv4 in GENERA_VLM_NETWORK; use attach mode for remote targets"))
      (%make-genera-runtime :mode (if (equal mode "local") :local :attach) :autostart autostart
        :root root :executable executable :world world :partition partition :debugger debugger
        :user user :network network :tap tap :guest guest :gateway gateway
        :state-directory state :log log))))

(defun genera-read-small-file (path &optional (limit 16384))
  (handler-case
      (with-open-file (in path)
        (with-output-to-string (out)
          (loop repeat limit for ch = (read-char in nil nil) while ch do (write-char ch out))))
    (error () nil)))

(defun genera-runtime-path (path root)
  (namestring (merge-pathnames path (uiop:ensure-directory-pathname root))))

#+linux
(defun genera-access-p (path mode)
  (handler-case (zerop (sb-posix:access path mode)) (error () nil)))

#+linux
(defun genera-file-fact (path)
  (handler-case
      (let* ((stat (sb-posix:stat path)) (mode (sb-posix:stat-mode stat)))
        (list :path (namestring (truename path)) :exists t :mode mode
              :kind (cond ((= (logand mode sb-posix:s-ifmt) sb-posix:s-ifreg) :file)
                          ((= (logand mode sb-posix:s-ifmt) sb-posix:s-ifdir) :directory)
                          ((= (logand mode sb-posix:s-ifmt) sb-posix:s-ifsock) :socket)
                          ((= (logand mode sb-posix:s-ifmt) sb-posix:s-ifchr) :character))
              :uid (sb-posix:stat-uid stat) :device (sb-posix:stat-dev stat)
              :inode (sb-posix:stat-ino stat) :size (sb-posix:stat-size stat)
              :read (genera-access-p path sb-posix:r-ok)
              :write (genera-access-p path sb-posix:w-ok)
              :execute (genera-access-p path sb-posix:x-ok)))
    (sb-posix:syscall-error (e)
      (list :path path :error (if (= (sb-posix:syscall-errno e) sb-posix:eacces)
                                 :access-denied :missing)))
    (error () (list :path path :error :unavailable))))

#+linux
(defun genera-existing-parent (path)
  (loop for directory = (uiop:pathname-directory-pathname path)
          then (uiop:pathname-parent-directory-pathname directory)
        for fact = (genera-file-fact (namestring directory))
        when (or (getf fact :exists) (equal (namestring directory) "/")) return fact))

(defun genera-display-socket (display)
  (when display
    (let* ((text (if (uiop:string-prefix-p "unix:" display) (subseq display 4) display))
           (parts (and (plusp (length text)) (char= (char text 0) #\:)
                       (uiop:split-string (subseq text 1) :separator "."))))
      (when (and (<= 1 (length parts) 2)
                 (every (lambda (part) (and (plusp (length part))
                                            (every (lambda (ch) (char<= #\0 ch #\9)) part))) parts))
        (format nil "/tmp/.X11-unix/X~D" (parse-integer (first parts)))))))

#+linux
(defun genera-elf64-p (path)
  (handler-case
      (with-open-file (in path :element-type '(unsigned-byte 8))
        (let ((header (make-array 20 :element-type '(unsigned-byte 8))))
          (and (= (read-sequence header in) 20)
               (equalp (subseq header 0 6) #(127 69 76 70 2 1))
               (= (aref header 18) 62) (= (aref header 19) 0))))
    (error () nil)))

#+linux
(defun genera-local-facts (runtime)
  (let* ((uid (sb-posix:geteuid))
         (display (uiop:getenv "DISPLAY")) (socket (genera-display-socket display))
         (tap-root (format nil "/sys/class/net/~A/" (genera-runtime-tap runtime)))
         (root (genera-runtime-root runtime))
         (paths (loop for key in '(:executable :world :partition :debugger)
                      for value in (list (genera-runtime-executable runtime) (genera-runtime-world runtime)
                                         (genera-runtime-partition runtime) (genera-runtime-debugger runtime))
                      append (list key (genera-file-fact (genera-runtime-path value root)))))
         (status (genera-read-small-file "/proc/self/status"))
         (groups (when status
                   (loop for line in (uiop:split-string status :separator '(#\Newline))
                         when (uiop:string-prefix-p "Groups:" line)
                           return (mapcar #'parse-integer
                                    (remove "" (uiop:split-string (subseq line 7)) :test #'equal))))))
    (labels ((number (name &optional (radix 10))
               (let ((text (genera-read-small-file (concatenate 'string tap-root name))))
                 (when text (ignore-errors (parse-integer text :start (if (= radix 16) 2 0)
                                                          :radix radix :junk-allowed t))))))
      (append (list :wsl (and (uiop:getenv "WSL_DISTRO_NAME")
                              (search "microsoft" (or (genera-read-small-file "/proc/sys/kernel/osrelease") "")
                                      :test #'char-equal) t)
                    :distribution (uiop:getenv "WSL_DISTRO_NAME")
                    :uid uid :real-uid (sb-posix:getuid)
                    :user (sb-posix:passwd-name (sb-posix:getpwuid uid))
                    :groups (adjoin (sb-posix:getegid) groups)
                    :display display :wayland-display (uiop:getenv "WAYLAND_DISPLAY")
                    :xdg-runtime-dir (uiop:getenv "XDG_RUNTIME_DIR")
                    :display-socket (and socket (genera-file-fact socket))
                    :root (genera-file-fact root)
                    :detacher (genera-file-fact "/usr/bin/setsid")
                    :tun (genera-file-fact "/dev/net/tun")
                    :tap-flags (number "tun_flags" 16) :link-flags (number "flags" 16)
                    :tap-owner (number "owner") :tap-group (number "group")
                    :state-directory (genera-file-fact (genera-runtime-state-directory runtime))
                    :state-parent (genera-existing-parent
                                   (uiop:ensure-directory-pathname (genera-runtime-state-directory runtime)))
                    :log (genera-file-fact (genera-runtime-log runtime))
                    :log-parent (genera-existing-parent (genera-runtime-log runtime))) paths
              (list :elf64 (genera-elf64-p (getf (getf paths :executable) :path)))))))

#-linux
(defun genera-local-facts (runtime) (declare (ignore runtime)) (list :wsl nil))

(defun genera-runtime-argv (runtime facts)
  "Fixed VLM arguments, with canonical verified file paths and no shell."
  (flet ((path (key) (getf (getf facts key) :path)))
    (list (path :executable) "-vm" "2000" "-debugger" (path :debugger)
          "-world" (path :world) "-network" (genera-runtime-network runtime)
          "-partition" (path :partition) "-foreground" "#F0EDCC" "-background" "#02343F")))

(defun genera-check-prerequisites (runtime facts)
  "Evaluate an inspection snapshot. This function performs no I/O."
  (let ((checks nil))
    (labels ((check (name success code message)
               (push (list :name name :ok (not (null success))
                           :code (if success :ready code) :message message) checks))
             (file-check (key modes)
               (let* ((fact (getf facts key)) (kind (getf fact :kind))
                      (missing (intern (concatenate 'string "MISSING-" (symbol-name key)) :keyword))
                      (invalid (intern (concatenate 'string "INVALID-" (symbol-name key)) :keyword)))
                 (check key
                        (and (eq kind :file) (plusp (getf fact :size 0))
                             (every (lambda (mode) (getf fact mode)) modes)
                             (or (not (eq key :executable)) (getf facts :elf64)))
                        (if (eq (getf fact :error) :missing) missing invalid)
                        (format nil "~A requires a nonempty regular file with ~{~A~^/~} access~A"
                                key modes (if (eq key :executable) " and Linux ELF64 x86-64 format" ""))))))
      (check :platform (getf facts :wsl) :not-wsl "Run the local runtime inside WSL")
      (when (getf facts :wsl)
        (check :user (and (eql (getf facts :uid) (getf facts :real-uid))
                          (not (eql (getf facts :uid) 0))
                          (equal (genera-runtime-user runtime) (getf facts :user)))
               :wrong-user "Run as the configured non-root WSL user")
        (let ((root (getf facts :root)))
          (check :root (and (eq (getf root :kind) :directory) (getf root :read) (getf root :execute))
                 :invalid-root "The VLM working directory must be readable and searchable"))
        (file-check :executable '(:read :execute))
        (file-check :world '(:read))
        (file-check :partition '(:read :write))
        (file-check :debugger '(:read :execute))
        (file-check :detacher '(:read :execute))
        (let ((socket (getf facts :display-socket)))
          (check :display (and (getf facts :display) (eq (getf socket :kind) :socket) (getf socket :write))
                 (if (getf facts :display) :display-unavailable :missing-display)
                 "Preserve a local WSLg DISPLAY with an accessible X11 socket"))
        (let ((tun (getf facts :tun)) (flags (getf facts :tap-flags)))
          (check :tap (and flags (logtest 2 flags) (logtest #x800 flags)
                           (getf facts :link-flags) (logtest 1 (getf facts :link-flags)))
                 :missing-tap "Provision a persistent TAP interface and bring it up before startup")
          (check :tap-access
                 (and (eq (getf tun :kind) :character) (getf tun :read) (getf tun :write)
                      ;; Linux tun_not_capable requires every configured UID/GID
                      ;; restriction to match. Do not assume CAP_NET_ADMIN.
                      (or (eql (getf facts :tap-owner) -1)
                          (eql (getf facts :tap-owner) (getf facts :uid)))
                      (or (eql (getf facts :tap-group) -1)
                          (member (getf facts :tap-group) (getf facts :groups))))
                 :tap-inaccessible "Satisfy all TAP owner/group restrictions and read/write access to /dev/net/tun"))
        (let ((log (getf facts :log)) (parent (getf facts :log-parent))
              (state (getf facts :state-directory)) (state-parent (getf facts :state-parent)))
          (check :state-directory
                 (and (eq (getf state-parent :kind) :directory)
                      (getf state-parent :write) (getf state-parent :execute)
                      (or (eq (getf state :error) :missing)
                          (and (eq (getf state :kind) :directory)
                               (eql (getf state :uid) (getf facts :uid))
                               (zerop (logand #o077 (getf state :mode 0))))))
                 :insecure-state-directory "Use a private, user-owned Linux state directory")
          (check :log
                 (and (eq (getf parent :kind) :directory) (getf parent :write) (getf parent :execute)
                      (or (eq (getf log :error) :missing)
                          (and (eq (getf log :kind) :file) (getf log :write)
                               (eql (getf log :uid) (getf facts :uid))
                               (zerop (logand #o077 (getf log :mode 0))))))
                 :log-inaccessible "Use a private writable log or a writable existing ancestor"))
        (let ((files (mapcar (lambda (key) (getf facts key))
                             '(:executable :world :partition :debugger :detacher :log))))
          (check :file-identities
                 (loop for (one . rest) on files always
                   (every (lambda (two)
                            (not (and (getf one :exists) (getf two :exists)
                                      (eql (getf one :device) (getf two :device))
                                      (eql (getf one :inode) (getf two :inode))))) rest))
                 :runtime-file-collision "Executable, world, partition, debugger, and log must be distinct files"))))
    (let ((ok (every (lambda (check) (getf check :ok)) checks)))
      (list :ok ok :mode :local :autostart (genera-runtime-autostart runtime)
            :launch-permitted (and ok (genera-runtime-autostart runtime))
            :root (getf (getf facts :root) :path) :log (genera-runtime-log runtime)
            :paths (loop for key in '(:executable :world :partition :debugger :detacher)
                         append (list key (getf (getf facts key) :path)))
            :log-info (getf facts :log)
            :state-directory (genera-runtime-state-directory runtime)
            :argv (and ok (genera-runtime-argv runtime facts))
            :environment (loop for key in '(:distribution :user :uid :display :wayland-display :xdg-runtime-dir)
                               append (list key (getf facts key)))
            :tap (list :name (genera-runtime-tap runtime) :owner (getf facts :tap-owner)
                       :group (getf facts :tap-group) :gateway (genera-runtime-gateway runtime))
            :checks (nreverse checks)))))

(defun genera-launch-preflight (runtime)
  "Inspect prerequisites only. Never launch, save a world, provision TAP, or run sudo."
  (unless runtime (error "Call CONFIGURE or supply a runtime configuration first"))
  (if (eq (genera-runtime-mode runtime) :attach)
      (list :ok t :mode :attach :autostart nil :launch-permitted nil :checks nil)
      (genera-check-prerequisites runtime (genera-local-facts runtime))))

#+linux
(progn
  (defvar *genera-launch-context* nil)

  ;; Linux flock locks an open file description, including between threads.
  ;; SB-POSIX:FLOCK names the fcntl structure, not the flock(2) function.
  (sb-alien:define-alien-routine ("flock" genera-linux-flock) sb-alien:int
    (fd sb-alien:int) (operation sb-alien:int))

  (defun genera-same-file-p (one two)
    (and (getf one :exists) (getf two :exists)
         (eql (getf one :device) (getf two :device))
         (eql (getf one :inode) (getf two :inode))))

  (defun genera-launch-target (preflight)
    (append (list :uid (sb-posix:geteuid)
                  :linux-boot (string-trim '(#\Newline #\Return)
                                (or (genera-read-small-file "/proc/sys/kernel/random/boot_id") "")))
            (loop for key in '(:executable :world :partition)
                  append (list key (genera-file-fact (getf (getf preflight :paths) key))))))

  (defun genera-proc-stat (text)
    ;; comm (field 2) can itself contain spaces and closing parentheses.
    (let* ((end (and text (position #\) text :from-end t)))
           (fields (and end (uiop:split-string (subseq text (+ end 2)))))
           (start (and (>= (length fields) 20)
                       (ignore-errors (parse-integer (nth 19 fields))))))
      (when start (list :start-time start :state (first fields)
                        :parent-pid (ignore-errors (parse-integer (second fields)))))))

  (defun genera-process-identity (pid target)
    "Read one stable /proc identity. Never signal, launch, or trust argv[0]."
    (let* ((base (format nil "/proc/~D/" pid))
           (before (genera-proc-stat (genera-read-small-file (concatenate 'string base "stat"))))
           (owner (genera-file-fact base)))
      (when (and before (not (member (getf before :state) '("Z" "X") :test #'equal)))
        (let* ((exe (genera-file-fact (concatenate 'string base "exe")))
               (cmd (genera-read-small-file (concatenate 'string base "cmdline") 65536))
               (argv (and cmd (uiop:split-string cmd :separator (string #\Null))))
               (cwd (ignore-errors (sb-posix:readlink (concatenate 'string base "cwd"))))
               (comm (string-trim '(#\Newline) (or (genera-read-small-file
                                                    (concatenate 'string base "comm")) "")))
               (world nil) (partition nil) (ambiguous nil))
          (labels ((argument (flag)
                     (let ((positions (loop for arg in (rest argv) for i from 1
                                            until (equal arg "--")
                                            when (equal arg flag) collect i)))
                       (unless (= (length positions) 1) (setf ambiguous t))
                       (let ((value (and positions (nth (1+ (first positions)) argv))))
                         (when (and value (plusp (length value))
                                    (or cwd (char= (char value 0) #\/)))
                           (genera-file-fact (genera-runtime-path value (or cwd "/"))))))))
            (setf world (argument "-world") partition (argument "-partition")))
          (let* ((after (genera-proc-stat (genera-read-small-file (concatenate 'string base "stat"))))
                 (relevant (or (genera-same-file-p exe (getf target :executable))
                               (genera-same-file-p world (getf target :world))
                               (genera-same-file-p partition (getf target :partition))
                               (let ((name (file-namestring (getf (getf target :executable) :path))))
                                 (or (equal comm (subseq name 0 (min 15 (length name))))
                                     (and (first argv) (equal (file-namestring (first argv)) name)))))))
            (when after
              (list :pid pid :uid (getf owner :uid) :start-time (getf before :start-time)
                    :parent-pid (getf before :parent-pid)
                    :linux-boot (getf target :linux-boot) :executable exe :world world :partition partition
                    :relevant (not (null relevant))
                    :unreadable (or (not (eql (getf before :start-time) (getf after :start-time)))
                                    (member (getf after :state) '("Z" "X") :test #'equal)
                                    (null cmd) (= (length cmd) 65536)
                                    (not (getf exe :exists)) ambiguous
                                    (not (getf world :exists)) (not (getf partition :exists))))))))))

  (defun genera-process-matches-p (process target)
    (and process (not (getf process :unreadable))
         (eql (getf process :uid) (getf target :uid))
         (every (lambda (key) (genera-same-file-p (getf process key) (getf target key)))
                '(:executable :world :partition))))

  (defun genera-discover-processes (target)
    "Include manual VLMs, aliases, foreign owners, and resource conflicts."
    (let ((matches nil) (conflicts nil))
      (dolist (directory (uiop:subdirectories #P"/proc/"))
        (let* ((name (car (last (pathname-directory directory))))
               (pid (and (stringp name) (plusp (length name))
                         (every #'digit-char-p name) (parse-integer name)))
               (process (and pid (genera-process-identity pid target))))
          (when (getf process :relevant)
            (if (genera-process-matches-p process target)
                (push process matches) (push process conflicts)))))
      (list :matches (sort matches #'< :key (lambda (p) (getf p :pid)))
            :conflicts (sort conflicts #'< :key (lambda (p) (getf p :pid))))))

  (defun genera-private-directory (path)
    (let* ((directory (uiop:ensure-directory-pathname path))
           (name (string-right-trim "/" (namestring directory))))
      (handler-case (sb-posix:lstat name)
        (sb-posix:syscall-error (e)
          (unless (= (sb-posix:syscall-errno e) sb-posix:enoent) (error e))
          (let ((parent (uiop:pathname-parent-directory-pathname directory)))
            (unless (probe-file parent) (genera-private-directory parent)))
          (handler-case (sb-posix:mkdir name #o700)
            (sb-posix:syscall-error (e)
              (unless (= (sb-posix:syscall-errno e) sb-posix:eexist) (error e))))))
      (let* ((stat (sb-posix:lstat name)) (mode (sb-posix:stat-mode stat)))
        (unless (and (sb-posix:s-isdir mode) (= (sb-posix:stat-uid stat) (sb-posix:geteuid))
                     (zerop (logand mode #o077)))
          (error "Insecure launch state directory: ~A" name)))
      (namestring directory)))

  (defun genera-private-open (path flags)
    ;; O_CLOEXEC is 02000000 in Linux asm-generic/fcntl.h.
    (let ((fd (sb-posix:open path (logior flags sb-posix:o-nofollow #o2000000) #o600)))
      (handler-case
          (let* ((stat (sb-posix:fstat fd)) (mode (sb-posix:stat-mode stat)))
            (unless (and (sb-posix:s-isreg mode) (= (sb-posix:stat-uid stat) (sb-posix:geteuid))
                         (= (sb-posix:stat-nlink stat) 1) (zerop (logand mode #o077)))
              (error "Insecure launch state file: ~A" path))
            fd)
        (error (e) (sb-posix:close fd) (error e)))))

  (defun genera-call-with-file-lock (path function)
    (let ((fd (genera-private-open path (logior sb-posix:o-rdwr sb-posix:o-creat))))
      (unwind-protect
          (if (zerop (genera-linux-flock fd 6)) ; LOCK_EX | LOCK_NB
              (funcall function)
              (if (= (sb-alien:get-errno) sb-posix:eagain)
                  (list :action :blocked :code :lock-busy)
                  (error "Cannot acquire launch lock: ~A" path)))
        ;; Closing releases flock, also after errors and nonlocal exits.
        (sb-posix:close fd))))

  (defun genera-identity-data (identity)
    (append (list 1 (getf identity :uid) (getf identity :pid) (getf identity :start-time)
                  (getf identity :linux-boot))
            (loop for key in '(:executable :world :partition)
                  for file = (getf identity key)
                  append (list (getf file :path) (getf file :device) (getf file :inode)))))

  (defun genera-read-launch-metadata (directory)
    (let* ((path (concatenate 'string directory "identity.json"))
           (fd (handler-case (genera-private-open path sb-posix:o-rdonly)
                 (sb-posix:syscall-error (e)
                   (if (= (sb-posix:syscall-errno e) sb-posix:enoent)
                       nil (error e))))))
      (when fd
        (with-open-stream (in (sb-sys:make-fd-stream fd :input t :external-format :utf-8 :auto-close t))
          (let ((text (make-string 16385)))
            (let ((count (read-sequence text in)))
              (when (> count 16384) (error "Oversized launch identity metadata"))
              (let* ((yason:*parse-json-arrays-as-vectors* nil)
                     (data (yason:parse (subseq text 0 count))))
                (unless (and (listp data) (= (length data) 14) (eql (first data) 1)
                             (every (lambda (i) (and (integerp (nth i data)) (>= (nth i data) 0)))
                                    '(1 2 3 6 7 9 10 12 13))
                             (plusp (third data))
                             (every (lambda (i) (stringp (nth i data))) '(4 5 8 11)))
                  (error "Invalid launch identity metadata"))
                data)))))))

  (defun genera-record-launch-identity (context identity)
    "Record a verified live VLM while CONTEXT's launch locks are held."
    (unless (and context (eq context *genera-launch-context*))
      (error "Launch identity requires held launch locks"))
    (let* ((target (getf context :target))
           (current (genera-process-identity (getf identity :pid) target))
           (directory (getf context :directory))
           (temporary (format nil "~Aidentity-~D-~D.tmp" directory (sb-posix:getpid) (random most-positive-fixnum))))
      (unless (and (genera-process-matches-p current target)
                   (equal (genera-identity-data identity) (genera-identity-data current)))
        (error "VLM identity changed before recording"))
      (unwind-protect
          (progn
            (with-open-stream
                (out (sb-sys:make-fd-stream
                       (genera-private-open temporary (logior sb-posix:o-wronly sb-posix:o-creat sb-posix:o-excl))
                       :output t :external-format :utf-8 :auto-close t))
              (yason:encode (coerce (genera-identity-data current) 'vector) out))
            (sb-posix:rename temporary (concatenate 'string directory "identity.json")))
        (when (probe-file temporary) (delete-file temporary)))))

  (defun genera-probe-launch-endpoint (runtime &optional (port 8888))
    "Presence only. A reachable TCP port is not a validated hello."
    (let ((socket nil))
      (unwind-protect
          (handler-case
              (sb-ext:with-timeout 0.5
                (setf socket (usocket:socket-connect (genera-runtime-guest runtime) port
                              :element-type '(unsigned-byte 8) :timeout 0.5))
                :present)
            (usocket:connection-refused-error () :absent)
            (usocket:timeout-error () :absent)
            (usocket:host-unreachable-error () :absent)
            (sb-ext:timeout () :absent)
            (error (e) (values :unknown (princ-to-string e))))
        (when socket (usocket:socket-close socket)))))

  (defun genera-launch-verdict (snapshot endpoint metadata)
    (let ((matches (getf snapshot :matches)) (conflicts (getf snapshot :conflicts)))
      (append
       (cond (conflicts (list :action :blocked :code :process-conflict))
             ((rest matches) (list :action :blocked :code :multiple-vlms))
             ((eq endpoint :present) (list :action :observe :code :endpoint-present))
             ((not (eq endpoint :absent)) (list :action :blocked :code :endpoint-unknown))
             (matches (list :action :observe :code :vlm-present))
             (t (list :action :launch :code :vlm-absent)))
       (list :process (first matches) :conflicts conflicts :matches matches :metadata metadata
             :endpoint endpoint))))

  (defun genera-call-with-launch-decision (runtime consumer &key
                                            (preflight (genera-launch-preflight runtime))
                                            (endpoint-observer #'genera-probe-launch-endpoint))
    "Call CONSUMER(decision, context) under both locks. Launch only there;
record the child's verified identity before returning. Never launch here."
    (unless (and (eq (genera-runtime-mode runtime) :local) (getf preflight :launch-permitted))
      (return-from genera-call-with-launch-decision (list :action :blocked :code :launch-disabled :preflight preflight)))
    (let* ((target (genera-launch-target preflight))
           (world (getf target :world))
           (state (genera-private-directory (genera-runtime-state-directory runtime)))
           (directory (genera-private-directory
                        (format nil "~Aworld-~D-~D/" state (getf world :device) (getf world :inode)))))
      ;; The user-wide guard also serializes different worlds sharing a partition.
      (genera-call-with-file-lock (concatenate 'string state "launch.lock")
        (lambda ()
          (genera-call-with-file-lock (concatenate 'string directory "launch.lock")
            (lambda ()
              (let* ((*genera-launch-context* (list :target target :directory directory))
                     (snapshot (genera-discover-processes target))
                     (endpoint (funcall endpoint-observer runtime))
                     (saved (genera-read-launch-metadata directory))
                     (recorded (and saved (genera-process-identity (third saved) target)))
                     (metadata (cond ((null saved) :missing)
                                     ((and (genera-process-matches-p recorded target)
                                           (equal saved (genera-identity-data recorded))) :current)
                                     (t :stale)))
                     (decision (genera-launch-verdict snapshot endpoint metadata)))
                (when (eq metadata :stale)
                  (delete-file (concatenate 'string directory "identity.json")))
                (when (and (eq metadata :current) (not (getf decision :process)))
                  (setf (getf decision :action) :blocked (getf decision :code) :identity-race))
                (when (and (getf decision :process) (not (eq (getf decision :action) :blocked)))
                  (genera-record-launch-identity *genera-launch-context* (getf decision :process)))
                (funcall consumer decision *genera-launch-context*))))))))

  (defstruct genera-launch-attempt process identity target root log observer-lost)

  (defun genera-launch-observation (attempt)
    "Observe this attempt only. Never respawn, stop, or replace a VLM."
    (let* ((process (genera-launch-attempt-process attempt))
           (status (sb-ext:process-status process))
           (saved (genera-launch-attempt-identity attempt))
           (current (and saved (genera-process-identity (getf saved :pid)
                                                       (genera-launch-attempt-target attempt))))
           (same (and saved current (not (getf current :unreadable))
                      (equal (genera-identity-data saved) (genera-identity-data current)))))
      (when (and same (member status '(:exited :signaled)))
        (setf (genera-launch-attempt-observer-lost attempt) t))
      (append
       (cond (same (list :state :running :code :vlm-started))
             ((and saved current) (list :state :unknown :code :identity-changed))
             ((member status '(:exited :signaled))
              (let ((known (and (not (genera-launch-attempt-observer-lost attempt))
                                (eq status :exited))))
                (list :state :exited :code :early-exit
                      :exit-code (and known (sb-ext:process-exit-code process)))))
             ((and saved (not same)) (list :state :unknown :code :identity-changed))
             (t (list :state :starting :code :identity-pending)))
       (list :attempted t :pid (getf saved :pid) :process saved
             :observer-lost (genera-launch-attempt-observer-lost attempt)
             :observer-status status
             :launcher-pid (sb-ext:process-pid process)
             :root (genera-launch-attempt-root attempt) :log (genera-launch-attempt-log attempt)))))

  (defun genera-open-launch-log (preflight context)
    (let* ((path (getf preflight :log))
           (parent (uiop:pathname-directory-pathname path))
           (directory (getf context :directory))
           (state (namestring (uiop:pathname-parent-directory-pathname directory))))
      (unless (probe-file parent) (genera-private-directory parent))
      (let ((canonical (namestring (merge-pathnames (file-namestring path) (truename parent)))))
        (when (or (equal canonical (concatenate 'string state "launch.lock"))
                  (uiop:string-prefix-p directory canonical))
          (error "VLM log collides with launch state: ~A" path)))
      (let ((fd (genera-private-open path (logior sb-posix:o-wronly sb-posix:o-creat sb-posix:o-append))))
        (handler-case
            (let ((stat (sb-posix:fstat fd)))
              (dolist (key '(:executable :world :partition :debugger :detacher))
                (let ((file (genera-file-fact (getf (getf preflight :paths) key))))
                  (when (and (= (sb-posix:stat-dev stat) (getf file :device -1))
                             (= (sb-posix:stat-ino stat) (getf file :inode -1)))
                    (error "VLM log aliases a runtime file: ~A" path))))
              (sb-sys:make-fd-stream fd :output t :element-type '(unsigned-byte 8) :auto-close t))
          (error (e) (sb-posix:close fd) (error e))))))

  (defun genera-spawn-vlm (preflight context &optional boot-deadline)
    "One structured setsid --wait launch, under the existing launch locks."
    (unless (and context (eq context *genera-launch-context*))
      (error "VLM launch requires held launch locks"))
    (let ((attempt nil))
      (handler-case
          (progn
            (with-open-stream (log (genera-open-launch-log preflight context))
              (setf attempt
                    (make-genera-launch-attempt
                     :process (sb-ext:run-program (getf (getf preflight :paths) :detacher)
                                (append '("--wait" "--") (getf preflight :argv))
                                :search nil :wait nil :pty nil :input nil :output log :error :output
                                :preserve-fds nil :directory (getf preflight :root))
                     :target (getf context :target) :root (getf preflight :root) :log (getf preflight :log))))
            ;; setsid can fork when SBCL makes its child a process-group leader.
            ;; --wait retains an exit-status observer; identify the actual VLM.
            (loop with deadline = (min (+ (get-internal-real-time) (* 2 internal-time-units-per-second))
                                       (if boot-deadline (* boot-deadline internal-time-units-per-second)
                                           most-positive-fixnum))
                  for observation = (genera-launch-observation attempt)
                  do (when (eq (getf observation :state) :exited) (return))
                     (let* ((snapshot (genera-discover-processes (getf context :target)))
                            (matches (getf snapshot :matches))
                            (pid (sb-ext:process-pid (genera-launch-attempt-process attempt)))
                            (identity (first matches)))
                       (when (or (getf snapshot :conflicts) (rest matches))
                         (error "VLM identity conflict after launch"))
                       (when (and identity (or (eql (getf identity :pid) pid)
                                              (eql (getf identity :parent-pid) pid)))
                         (genera-record-launch-identity context identity)
                         (setf (genera-launch-attempt-identity attempt) identity)
                         (return)))
                     (when (>= (get-internal-real-time) deadline) (return))
                     (sleep 0.01))
            (when (genera-launch-attempt-identity attempt)
              (let ((remaining (and boot-deadline
                                    (- boot-deadline (/ (get-internal-real-time) internal-time-units-per-second)))))
                (sleep (if remaining (max 0 (min .2 remaining)) .2))))
            (let ((observation (genera-launch-observation attempt)))
              (values (append (list :action (if (eq (getf observation :state) :running) :launched :failed))
                              observation) attempt)))
        (error (e)
          ;; A short-lived child can exit between identity reads/recording.
          (when attempt (sleep 0.02))
          (let ((observation (and attempt (genera-launch-observation attempt))))
            (values
             (if (eq (getf observation :state) :exited)
                 (append (list :action :failed :message (princ-to-string e)) observation)
                 (append (list :action :failed :code :launch-failed :message (princ-to-string e)
                               :attempted (not (null attempt)) :log (getf preflight :log))
                         (when attempt (list :launcher-pid
                                              (sb-ext:process-pid (genera-launch-attempt-process attempt))))))
             attempt))))))

  (defun genera-launch-vlm (runtime &key (preflight (genera-launch-preflight runtime))
                                        (endpoint-observer #'genera-probe-launch-endpoint) before-launch deadline)
    "Discover first; launch at most once. Return report and observation handle."
    (let ((attempt nil))
      (values
       (genera-call-with-launch-decision runtime
         (lambda (decision context)
           (if (eq (getf decision :action) :launch)
               (progn
                 (when before-launch (funcall before-launch))
                 (multiple-value-bind (report handle) (genera-spawn-vlm preflight context deadline)
                   (setf attempt handle) report))
               decision))
         :preflight preflight :endpoint-observer endpoint-observer)
       attempt))))
