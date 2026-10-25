;;; Read-only launch configuration and prerequisite inspection for SBCL/WSL.

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
                               (eql (getf log :uid) (getf facts :uid)))))
                 :log-inaccessible "The log must be writable or have a writable existing ancestor"))
        (let ((files (mapcar (lambda (key) (getf facts key)) '(:executable :world :partition :debugger :log))))
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
            :paths (loop for key in '(:executable :world :partition :debugger)
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
