;;; Explicit source mappings. No runtime-directory or transfer fallback.
(in-package #:lisp-mcp)
#+linux (eval-when (:compile-toplevel :load-toplevel :execute) (require :sb-posix))

(defconstant +genera-source-limit+ 16777216)
(defstruct genera-source-mapping host-root windows-root genera-root)
(defstruct genera-source-path original kind host-path genera-path
  canonical-root canonical-path boot-id service-id proof-request-id)

(define-condition genera-path-error (error)
  ((kind :initarg :kind :reader genera-path-error-kind)
   (path :initarg :path :reader genera-path-error-path)
   (detail :initarg :detail :reader genera-path-error-detail))
  (:report (lambda (condition out)
    (let ((path (genera-path-error-path condition)))
      (format out "Source path ~S (~A): ~A. Host attempt: ~S; Genera attempt: ~S"
              (genera-source-path-original path) (genera-source-path-kind path)
              (genera-path-error-detail condition) (genera-source-path-host-path path)
              (genera-source-path-genera-path path))))))

(defun genera-path-fail (path kind detail)
  (error 'genera-path-error :kind kind :path path :detail detail))

(defun genera-source-component-p (part)
  ;; Exclude wildcards, quoting, version selectors, drives, and traversal.
  (and (plusp (length part)) (not (member part '("." "..") :test #'equal))
       (not (find (char part (1- (length part))) '(#\Space #\.)))
       (every (lambda (ch) (or (char<= #\a ch #\z) (char<= #\A ch #\Z)
                               (char<= #\0 ch #\9) (find ch "_-.+ "))) part)))

(defun genera-source-parts (text separator)
  (let ((parts (uiop:split-string text :separator (string separator))))
    (and (every #'genera-source-component-p parts) parts)))

(defun genera-source-windows-p (text)
  (and (>= (length text) 3) (alpha-char-p (char text 0))
       (char= (char text 1) #\:) (find (char text 2) "/\\")))

(defun genera-source-native-root-p (text)
  (let* ((colon (position #\: text)) (separator (char text (1- (length text))))
         (tail (and colon (subseq text (1+ colon) (1- (length text))))))
    (and colon (> colon 0) (not (find #\: text :start (1+ colon)))
         (every (lambda (ch) (or (char<= #\a ch #\z) (char<= #\A ch #\Z)
                                 (char<= #\0 ch #\9) (find ch "_-."))) (subseq text 0 colon))
         (find separator "/>;") tail
         (if (find separator "/>")
             (and (plusp (length tail)) (char= (char tail 0) separator)
                  (or (= (length tail) 1) (genera-source-parts (subseq tail 1) separator)))
             (genera-source-parts tail separator)))))

(defun genera-source-configuration (profile protocol)
  "Read mapping syntax only; discovery does not inspect files or Genera."
  (let ((host (configuration-text "GENERA_SOURCE_ROOT"))
        (windows (configuration-text "GENERA_SOURCE_WINDOWS_ROOT"))
        (native (configuration-text "GENERA_SOURCE_GENERA_ROOT")))
    (when (or host windows native)
      (unless (and (equal profile "genera") (equal protocol "v1"))
        (configuration-error "GENERA_SOURCE_GENERA_ROOT" "requires the genera profile and v1"))
      (unless (and native (genera-source-native-root-p native))
        (configuration-error "GENERA_SOURCE_GENERA_ROOT" "requires a literal host-qualified directory ending in /, >, or ;"))
      (when host
        (unless (and (char= (char host 0) #\/) (not (find #\\ host))
                     (genera-source-parts (string-trim "/" host) #\/))
          (configuration-error "GENERA_SOURCE_ROOT" "requires an absolute literal Unix source directory")))
      (when windows
        (let ((normalized (substitute #\/ #\\ windows)))
          (unless (and host (genera-source-windows-p normalized)
                       (genera-source-parts (string-right-trim "/" (subseq normalized 3)) #\/))
            (configuration-error "GENERA_SOURCE_WINDOWS_ROOT" "requires a drive-qualified source directory and GENERA_SOURCE_ROOT"))
          (setf windows (concatenate 'string (string-right-trim "/" normalized) "/"))))
      (make-genera-source-mapping :host-root host :windows-root windows :genera-root native))))

(defun genera-source-native-file-p (text)
  (let ((separator (position-if (lambda (ch) (find ch "/>;")) text :from-end t)))
    (and separator (genera-source-native-root-p (subseq text 0 (1+ separator)))
         (genera-source-component-p (subseq text (1+ separator))))))

(defun genera-source-under-p (path root &optional case-insensitive)
  (and (<= (length root) (length path))
       (if case-insensitive (string-equal root path :end2 (length root))
           (string= root path :end2 (length root)))))

(defun genera-source-native-case-p (root)
  (not (char= (char root (1+ (position #\: root))) #\/)))

(defun genera-map-source (kind original &optional (mapping *genera-source-mapping*))
  "Validate local paths and construct an explicit, still-unverified target."
  (let* ((path (make-genera-source-path :original original :kind kind))
         (root (and mapping (genera-source-mapping-host-root mapping)))
         (native (and mapping (genera-source-mapping-genera-root mapping)))
         (relative nil))
    (unless (and (member kind '("genera" "host" "relative") :test #'equal)
                 (stringp original) (<= 1 (length original) 4096))
      (genera-path-fail path :invalid-path "Expected a pathname and explicit genera, host, or relative kind"))
    (unless native (genera-path-fail path :mapping-missing "No Genera source root is configured"))
    (cond
      ((equal kind "genera")
       (setf (genera-source-path-genera-path path) original)
       ;; Logical and physical aliases are checked after native translation.
       (unless (genera-source-native-file-p original)
         (genera-path-fail path :invalid-path "Native path must name a literal file")))
      (t
       (unless root (genera-path-fail path :mapping-missing "No host source root is configured"))
       (let ((host-root (concatenate 'string (string-right-trim "/" root) "/")))
         (setf relative
           (cond ((equal kind "relative") original)
                 ((genera-source-windows-p original)
                  (let* ((windows (genera-source-mapping-windows-root mapping))
                         (normalized (substitute #\/ #\\ original)))
                    (setf (genera-source-path-host-path path) normalized)
                    (unless (and windows (genera-source-under-p normalized windows t))
                      (genera-path-fail path :outside-root "Windows path has no matching configured alias"))
                    (subseq normalized (length windows))))
                 ((genera-source-under-p original host-root) (subseq original (length host-root)))
                 (t (setf (genera-source-path-host-path path) original)
                    (genera-path-fail path :outside-root "Host path is outside the configured source root"))))
         (setf (genera-source-path-host-path path) (concatenate 'string host-root relative)
               (genera-source-path-genera-path path)
               (concatenate 'string native (substitute (char native (1- (length native))) #\/ relative)))
         (unless (genera-source-parts relative #\/)
           (genera-path-fail path :invalid-path "Path contains traversal, an empty component, or unsupported syntax"))
         (let* ((canonical-root (ignore-errors (namestring (truename (uiop:ensure-directory-pathname host-root)))))
                (canonical (ignore-errors (namestring (truename (genera-source-path-host-path path))))))
           (unless (and canonical-root canonical (not (uiop:directory-pathname-p canonical))
                        #+linux (ignore-errors (sb-posix:s-isreg (sb-posix:stat-mode (sb-posix:stat canonical)))))
             (genera-path-fail path :file-missing "Host source root or regular source file does not exist"))
           (unless (genera-source-under-p canonical canonical-root)
             (genera-path-fail path :outside-root "Resolved host path escapes the source root"))
           (setf (genera-source-path-host-path path) canonical)))))
    path))

(defun genera-source-fingerprint (file)
  "Adler-32 of ASCII text with CR, LF, and CRLF normalized to LF."
  (with-open-file (in file :element-type '(unsigned-byte 8))
    (when (> (file-length in) +genera-source-limit+) (error "Source exceeds 16 MiB"))
    (let ((a 1) (b 0) (size 0) (raw 0) (cr nil))
      (loop for code = (read-byte in nil nil) while code
            do (when (> (incf raw) +genera-source-limit+) (error "Source exceeds 16 MiB"))
               (unless (or (<= 32 code 126) (member code '(9 10 13)))
                 (error "Source requires unsupported character conversion"))
               (unless (and cr (= code 10))
                 (incf size)
                 (setf a (mod (+ a (if (= code 13) 10 code)) 65521) b (mod (+ b a) 65521)))
               (setf cr (= code 13)))
      (list size (+ (ash b 16) a)))))

(defun genera-load-fields (kind original &optional (mapping *genera-source-mapping*))
  "Prepare local mapping and fingerprint; native verification stays in LOAD."
  (let* ((path (genera-map-source kind original mapping))
         (host (genera-source-path-host-path path))
         (fingerprint (when host
                        (handler-case (genera-source-fingerprint host)
                          (error (e) (genera-path-fail path :source-unreadable (princ-to-string e)))))))
    (values (append (list :original-path original :path-kind kind
                          :path (genera-source-path-genera-path path)
                          :source-root (genera-source-mapping-genera-root mapping))
                    (when host (list :host-path host :source-size (first fingerprint)
                                     :source-hash (second fingerprint)))) path)))

(defun genera-resolve-source (session kind original &optional (mapping *genera-source-mapping*))
  "Require a tracked native probe before returning a verified pathname."
  (let* ((path (genera-map-source kind original mapping))
         (local (when (genera-source-path-host-path path)
                  (handler-case (genera-source-fingerprint (genera-source-path-host-path path))
                    (error (e) (genera-path-fail path :source-unreadable (princ-to-string e)))))))
    (unless session (genera-path-fail path :verification-required "A ready Genera session is required; no transfer was attempted"))
    (handler-case
        (let* ((generation (sb-thread:with-mutex ((genera-session-lock session))
                             (let ((hello (genera-session-hello session)))
                               (list (getf hello :boot-id) (getf hello :service-id)))))
               (request (genera-submit-request session :eval :package "COMMON-LISP-USER"
                          :expected-generation generation :host-timeout 5 :print-level 3 :print-length 8
                          :form (format nil "(mcp-probe-source ~S ~S)"
                                  (genera-source-mapping-genera-root mapping) (genera-source-path-genera-path path))))
               (response (progn
                           (setf (genera-source-path-proof-request-id path)
                                 (getf (genera-request-envelope request) :id)
                                 (genera-source-path-boot-id path) (first generation)
                                 (genera-source-path-service-id path) (second generation))
                           (genera-await-request session request))))
          (unless (and (getf response :ok) (= (length (getf response :values)) 1))
            (genera-path-fail path :verification-failed
              (or (getf (getf response :error) :message) "Native file verification failed")))
          (let ((proof (mcp-v1-parse (first (getf response :values)))))
            (unless (and (listp proof) (= (length proof) 4) (stringp (first proof))
                         (stringp (second proof)) (integerp (third proof)) (integerp (fourth proof))
                         (<= 0 (third proof) +genera-source-limit+) (<= 0 (fourth proof) 4294967295)
                         (genera-source-native-root-p (first proof))
                         (genera-source-native-file-p (second proof))
                         (genera-source-under-p (second proof) (first proof)
                                               (genera-source-native-case-p (first proof))))
              (genera-path-fail path :verification-failed "Invalid canonical native path proof"))
            (when (and local (not (equal local (cddr proof))))
              (genera-path-fail path :source-mismatch "Host and Genera source fingerprints differ; synchronize explicitly"))
            (setf (genera-source-path-canonical-root path) (first proof)
                  (genera-source-path-canonical-path path) (second proof)
                  (genera-source-path-boot-id path) (first generation)
                  (genera-source-path-service-id path) (second generation))
            path))
      (genera-session-error (e) (genera-path-fail path :verification-failed (princ-to-string e)))
      (genera-path-error (e) (error e))
      (error () (genera-path-fail path :verification-failed "Invalid native verification response")))))
